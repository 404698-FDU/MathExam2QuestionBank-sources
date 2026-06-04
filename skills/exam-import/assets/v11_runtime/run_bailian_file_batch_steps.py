#!/usr/bin/env python3
"""Run Step3, Step3.5, and Step4 through Bailian OpenAI-compatible Batch API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import requests
from pydantic import ValidationError

WORKTREE_ROOT = Path(__file__).resolve().parent
CODE_ROOT = WORKTREE_ROOT.parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import step3_question2json as step3  # noqa: E402
import step3_5_full_latex_normalize as step35  # noqa: E402
import step4_asset_distribution as step4  # noqa: E402
from common_io import read_json, write_json, write_jsonl  # noqa: E402
from common_llm import BAILIAN_TOKEN_FILES, first_token  # noqa: E402
from step3_5_question_json_audit_fix import audit_record, field_set, load_question_bank  # noqa: E402
from step3_schema import StrictStep3Record  # noqa: E402


BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def load_bailian_token() -> str:
    token = (
        os.environ.get("DASHSCOPE_API_KEY", "").strip()
        or os.environ.get("BAILIAN_API_KEY", "").strip()
        or os.environ.get("LLM_API_KEY", "").strip()
        or first_token(BAILIAN_TOKEN_FILES)
    )
    if not token:
        raise RuntimeError("missing Bailian/DashScope API key")
    return token.removeprefix("Bearer ").strip()


class BailianBatchClient:
    def __init__(self, token: str, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, url: str, *, timeout: int, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, 5):
            try:
                return requests.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 4:
                    break
                time.sleep(2 * attempt)
        raise RuntimeError(f"{method} {url} failed after retries: {last_error}") from last_error

    def upload_jsonl(self, path: Path) -> dict[str, Any]:
        with path.open("rb") as handle:
            response = self._request(
                "POST",
                f"{self.base_url}/files",
                headers=self.headers,
                data={"purpose": "batch"},
                files={"file": (path.name, handle, "application/jsonl")},
                timeout=300,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"upload failed HTTP {response.status_code}: {response.text[:1200]}")
        return response.json()

    def create_batch(self, input_file_id: str, name: str, description: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"{self.base_url}/batches",
            headers={**self.headers, "Content-Type": "application/json"},
            json={
                "input_file_id": input_file_id,
                "endpoint": "/v1/chat/completions",
                "completion_window": "24h",
                "metadata": {"ds_name": name, "ds_description": description},
            },
            timeout=120,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"create batch failed HTTP {response.status_code}: {response.text[:1200]}")
        return response.json()

    def retrieve_batch(self, batch_id: str) -> dict[str, Any]:
        response = self._request("GET", f"{self.base_url}/batches/{batch_id}", headers=self.headers, timeout=120)
        if response.status_code >= 400:
            raise RuntimeError(f"retrieve batch failed HTTP {response.status_code}: {response.text[:1200]}")
        return response.json()

    def wait_batch(self, batch_id: str, out_dir: Path, poll_seconds: int, timeout_seconds: int) -> dict[str, Any]:
        started = time.monotonic()
        statuses: list[dict[str, Any]] = []
        while True:
            status = self.retrieve_batch(batch_id)
            statuses.append(status)
            write_json(out_dir / f"{batch_id}_status_latest.json", status)
            terminal = str(status.get("status") or "") in {"completed", "failed", "expired", "cancelled"}
            if terminal:
                write_json(out_dir / f"{batch_id}_status_history.json", statuses)
                return status
            if time.monotonic() - started > timeout_seconds:
                write_json(out_dir / f"{batch_id}_status_history.json", statuses)
                raise TimeoutError(f"batch {batch_id} did not finish in {timeout_seconds}s")
            time.sleep(poll_seconds)

    def download_file(self, file_id: str, path: Path) -> None:
        response = self._request("GET", f"{self.base_url}/files/{file_id}/content", headers=self.headers, timeout=600)
        if response.status_code >= 400:
            raise RuntimeError(f"download file failed HTTP {response.status_code}: {response.text[:1200]}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)


def write_batch_requests(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_batch_output(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            rows[str(row.get("custom_id") or "")] = row
    return rows


def tool_body(
    model: str,
    messages: list[dict[str, Any]],
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    *,
    temperature: float = 0.0,
    top_p: float = 0.8,
    enable_thinking: bool = False,
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "enable_thinking": enable_thinking,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": tool_description,
                    "parameters": tool_schema,
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": tool_name}},
    }


def extract_tool_arguments(row: dict[str, Any], tool_name: str) -> str:
    if row.get("error"):
        raise RuntimeError(f"batch row error: {json.dumps(row.get('error'), ensure_ascii=False)[:1200]}")
    response = row.get("response") or {}
    status_code = int(response.get("status_code") or 0)
    if status_code >= 400:
        raise RuntimeError(f"batch row HTTP {status_code}: {json.dumps(response.get('body'), ensure_ascii=False)[:1200]}")
    body = response.get("body") or {}
    message = body["choices"][0]["message"]
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError(f"missing tool_calls: {json.dumps(body, ensure_ascii=False)[:1200]}")
    function = tool_calls[0].get("function") or {}
    if function.get("name") != tool_name:
        raise RuntimeError(f"unexpected tool name: {function.get('name')}")
    return function["arguments"]


def run_batch(client: BailianBatchClient, requests_path: Path, out_dir: Path, name: str, description: str, poll_seconds: int, timeout_seconds: int, resume_batch_id: str = "") -> tuple[dict[str, Any], Path]:
    if resume_batch_id:
        batch_id = resume_batch_id
    else:
        upload = client.upload_jsonl(requests_path)
        write_json(out_dir / f"{name}_upload.json", upload)
        batch = client.create_batch(str(upload["id"]), name=name, description=description)
        write_json(out_dir / f"{name}_create.json", batch)
        batch_id = str(batch["id"])
    final = client.wait_batch(batch_id, out_dir=out_dir, poll_seconds=poll_seconds, timeout_seconds=timeout_seconds)
    output_file_id = final.get("output_file_id")
    if not output_file_id:
        raise RuntimeError(f"batch did not produce output_file_id: {json.dumps(final, ensure_ascii=False)[:1200]}")
    output_path = out_dir / f"{name}_output.jsonl"
    client.download_file(str(output_file_id), output_path)
    if final.get("error_file_id"):
        client.download_file(str(final["error_file_id"]), out_dir / f"{name}_errors.jsonl")
    return final, output_path


def step3_request_for_payload(payload: dict[str, Any], runs_root: Path, out_dir: Path, model: str, no_ocr_input: bool) -> tuple[str, dict[str, Any], dict[str, Any]]:
    qno = int(payload["question_no"])
    per_dir = out_dir / "per_question"
    input_path = per_dir / f"q{qno:02d}_input.json"
    source_path = per_dir / f"q{qno:02d}_source_payload.json"
    compact = step3.minimal_payload_for_model(payload, no_ocr_input=no_ocr_input)
    audit_input = dict(compact)
    audit_input["step2_question_range_labels"] = step3.question_surface_crop_labels(payload)
    question_crop_images = step3.prepare_step3_question_crop_images(payload, runs_root, out_dir)
    answer_crop_images = step3.prepare_step3_answer_crop_images(payload, runs_root, out_dir)
    audit_input["question_crop_images"] = [str(path.resolve()) for path in question_crop_images]
    audit_input["answer_crop_images"] = [str(path.resolve()) for path in answer_crop_images]
    step3.write_json(input_path, audit_input)
    step3.write_json(source_path, payload)
    messages = step3.build_step3_messages(compact, question_crop_images, answer_crop_images)
    image_only = bool(no_ocr_input)
    tool_name = "submit_step3_image_only_record" if image_only else "submit_step3_record"
    schema = step3.STEP3_IMAGE_ONLY_TOOL_SCHEMA if image_only else step3.STEP3_TOOL_SCHEMA
    description = "提交从题面图和答案解析图整理出的单题题库记录。" if image_only else "提交一道数学试题的 Step3 结构化记录。"
    tool_messages = [
        *messages,
        {"role": "user", "content": f"必须调用 {tool_name} 工具提交结果；不要在 message.content 输出 JSON、Markdown 或解释文字。"},
    ]
    body = tool_body(model, tool_messages, tool_name, description, schema, temperature=0.1, enable_thinking=False)
    custom_id = f"step3-q{qno:02d}"
    return custom_id, {"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions", "body": body}, {"payload": payload, "compact": compact, "tool_name": tool_name}


def run_step3_batch(args: argparse.Namespace, client: BailianBatchClient) -> dict[str, Any]:
    run_id = args.run_id
    runs_root = args.runs_root
    out_dir = args.output_root / "question_bank" / run_id
    payloads = step3.build_payloads_for_run(run_id, runs_root)
    pipeline_summary = step3.read_json(runs_root / f"{run_id}_raw_units" / "pipeline_summary.json", {})
    pipeline_mode = str(pipeline_summary.get("mode") or "paper_plus_answer_file")
    for payload in payloads:
        payload["pipeline_mode"] = pipeline_mode
    payloads.sort(key=lambda payload: (-step3.estimate_task_weight(payload), int(payload["question_no"])))
    requests_list: list[dict[str, Any]] = []
    meta: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        custom_id, request, request_meta = step3_request_for_payload(payload, runs_root, out_dir, args.model, args.no_ocr_input)
        requests_list.append(request)
        meta[custom_id] = request_meta
    write_json(out_dir / "queue_plan.json", {"strategy": "single_file_batch", "items": [{"question_no": int(p["question_no"])} for p in payloads]})
    requests_path = args.output_root / "batch_jobs" / "step3_requests.jsonl"
    write_batch_requests(requests_path, requests_list)
    started = time.monotonic()
    final, output_path = run_batch(client, requests_path, args.output_root / "batch_jobs", "step3", f"{run_id} Step3", args.poll_seconds, args.batch_timeout, resume_batch_id=args.resume_step3_batch_id)
    output_rows = read_batch_output(output_path)
    results: list[dict[str, Any]] = []
    records_by_qno: dict[int, dict[str, Any]] = {}
    for custom_id, request_meta in meta.items():
        payload = request_meta["payload"]
        compact = request_meta["compact"]
        qno = int(payload["question_no"])
        per_dir = out_dir / "per_question"
        raw_path = per_dir / f"q{qno:02d}_raw.json.txt"
        validated_path = per_dir / f"q{qno:02d}_validated.json"
        parsed_path = per_dir / f"q{qno:02d}.json"
        row_started = time.monotonic()
        try:
            raw = extract_tool_arguments(output_rows[custom_id], request_meta["tool_name"])
            raw_path.write_text(raw, encoding="utf-8", newline="\n")
            parsed, structural_repairs = step3.normalize_structural_fields(json.loads(raw))
            validated = step3.Step3Record.model_validate(parsed)
            if int(validated.question_no) != qno:
                raise ValueError(f"question_no mismatch: expected {qno}, got {validated.question_no}")
            warnings = step3.content_warnings(validated, compact)
            step3.write_json(validated_path, validated.model_dump(mode="json"))
            record = step3.normalize_schema_record(validated, payload, model=args.model, elapsed=0.0, warnings=warnings)
            step3.write_json(parsed_path, record)
            records_by_qno[qno] = record
            results.append(
                {
                    "question_no": qno,
                    "status": "ok_with_warnings" if warnings else "ok",
                    "question_type": record.get("question_type"),
                    "elapsed_seconds": round(time.monotonic() - row_started, 3),
                    "warning_count": len(warnings),
                    "warnings": warnings,
                    "structural_repairs": structural_repairs,
                    "record": record,
                }
            )
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            record = step3.error_record(payload, model=args.model, message=error, elapsed=0.0)
            record["schema_version"] = "step3_json_schema_v1"
            step3.write_json(parsed_path, record)
            records_by_qno[qno] = record
            results.append({"question_no": qno, "status": "error", "elapsed_seconds": 0.0, "error": error, "traceback": traceback.format_exc(), "record": record})
    records = [records_by_qno[qno] for qno in sorted(records_by_qno)]
    summary = step3.summarize(run_id, records, results, out_dir, args.model)
    summary["success_count"] = sum(1 for result in results if str(result.get("status") or "").startswith("ok"))
    summary["error_count"] = sum(1 for result in results if str(result.get("status") or "") == "error")
    summary["batch_api"] = {"mode": "file_batch", "batch": final, "wall_seconds": round(time.monotonic() - started, 3)}
    step3.write_json(out_dir / "summary.json", summary)
    return summary


def step35_request(record: dict[str, Any], findings: list[dict[str, Any]], model: str) -> tuple[str, dict[str, Any]]:
    qno = int(record["question_no"])
    body = tool_body(
        model,
        [
            {"role": "system", "content": step35.SYSTEM_PROMPT},
            {"role": "user", "content": step35.USER_PROMPT + json.dumps({"record": record, "audit_findings": findings}, ensure_ascii=False, indent=2)},
        ],
        "submit_step3_5_record",
        "提交完整规范化后的 Step3.5 单题记录。",
        step35.STEP3_RECORD_TOOL_SCHEMA,
        temperature=0.0,
        enable_thinking=False,
    )
    custom_id = f"step35-q{qno:02d}"
    return custom_id, {"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions", "body": body}


def run_step35_batch(args: argparse.Namespace, client: BailianBatchClient) -> dict[str, Any]:
    run_id = args.run_id
    run_dir = args.output_root / "question_bank" / run_id
    out_dir = args.output_root / "step3_5_full_latex_normalize" / run_id
    fields = field_set("all")
    records = step35.load_records(run_dir, None, "question_bank")
    before_findings = {int(record["question_no"]): audit_record(record, fields) for record in records}
    write_json(out_dir / "audit_before.json", {"run_id": run_id, "question_numbers": [int(r["question_no"]) for r in records], "field_scope": sorted(fields)})
    requests_list: list[dict[str, Any]] = []
    record_by_custom: dict[str, dict[str, Any]] = {}
    for record in records:
        qno = int(record["question_no"])
        per_dir = out_dir / "per_question"
        write_json(per_dir / f"q{qno:02d}_before.json", record)
        write_json(per_dir / f"q{qno:02d}_findings_before.json", before_findings[qno])
        custom_id, request = step35_request(record, before_findings[qno], args.model)
        requests_list.append(request)
        record_by_custom[custom_id] = record
    requests_path = args.output_root / "batch_jobs" / "step35_requests.jsonl"
    write_batch_requests(requests_path, requests_list)
    started = time.monotonic()
    final, output_path = run_batch(client, requests_path, args.output_root / "batch_jobs", "step35", f"{run_id} Step3.5", args.poll_seconds, args.batch_timeout, resume_batch_id=args.resume_step35_batch_id)
    rows = read_batch_output(output_path)
    results: list[dict[str, Any]] = []
    normalized_by_qno: dict[int, dict[str, Any]] = {}
    for custom_id, record in record_by_custom.items():
        qno = int(record["question_no"])
        per_dir = out_dir / "per_question"
        try:
            raw = extract_tool_arguments(rows[custom_id], "submit_step3_5_record")
            fixed = StrictStep3Record.model_validate(json.loads(raw)).model_dump(mode="json")
            if int(fixed.get("question_no") or 0) != qno:
                raise ValueError(f"question_no mismatch: expected {qno}, got {fixed.get('question_no')}")
            write_json(per_dir / f"q{qno:02d}_normalized.json", fixed)
            (per_dir / f"q{qno:02d}_raw_after.json.txt").write_text(raw, encoding="utf-8", newline="\n")
            normalized_by_qno[qno] = fixed
            results.append({"question_no": qno, "status": "normalized", "before_finding_count": len(before_findings[qno]), "model": args.model, "structured_output": "tool_calling"})
        except Exception as exc:  # noqa: BLE001
            results.append({"question_no": qno, "status": "error", "before_finding_count": len(before_findings[qno]), "error": f"{type(exc).__name__}: {exc}", "model": args.model, "structured_output": "tool_calling"})
    merged_records, applied_qnos, rejected_qnos = step35.merge_normalized_fields(records, normalized_by_qno, results, fields)
    summary = {
        "run_id": run_id,
        "source_stage": "question_bank",
        "model": args.model,
        "worker_models": [args.model],
        "enable_thinking": False,
        "structured_output": "tool_calling",
        "field_scope": sorted(fields),
        "question_count": len(records),
        "before_finding_count": sum(len(items) for items in before_findings.values()),
        "normalized_count": sum(1 for item in results if str(item.get("status") or "").startswith("normalized")),
        "error_count": sum(1 for item in results if str(item.get("status") or "") == "error"),
        "applied_question_numbers": applied_qnos,
        "rejected_question_numbers": rejected_qnos,
        "write_back": True,
        "batch_api": {"mode": "file_batch", "batch": final, "wall_seconds": round(time.monotonic() - started, 3)},
        "results": sorted(results, key=lambda item: int(item.get("question_no") or 0)),
    }
    write_json(out_dir / "normalized_question_bank.json", [normalized_by_qno.get(int(r["question_no"]), r) for r in records])
    write_json(out_dir / "merged_question_bank.json", merged_records)
    write_jsonl(out_dir / "merged_question_bank.jsonl", merged_records)
    write_json(out_dir / "summary.json", summary)
    step35.write_back_question_bank(run_dir, merged_records, summary)
    return summary


def run_step4_batch(args: argparse.Namespace, client: BailianBatchClient) -> dict[str, Any]:
    run_id = args.run_id
    runs_root = args.runs_root
    qb_root = args.output_root / "question_bank"
    run_dir = runs_root / f"{run_id}_raw_units"
    summary_path = run_dir / "pipeline_summary.json"
    pipeline_summary = read_json(summary_path, {})
    pipeline_mode = str(pipeline_summary.get("mode") or "pure_paper")
    rows = (read_json(run_dir / "qa_alignment.json", {}) or {}).get("qa_alignment") or []
    question_packets = step4.load_packet_dir(run_dir, "question_packets")
    answer_packets = step4.load_packet_dir(run_dir, "answer_packets")
    question_blocks = step4.block_by_label(question_packets)
    answer_blocks = step4.block_by_label(answer_packets)
    question_groups = read_json(run_dir / "question_groups.json", [])
    packet_sources = [("mixed", question_packets, question_groups)] if pipeline_mode == "mixed" else [("question", question_packets, question_groups)]
    if pipeline_mode == "paper_plus_answer_file" and answer_packets:
        packet_sources.append(("answer", answer_packets, step4.groups_for_stream(rows, "answer", question_groups)))
    contexts: list[dict[str, Any]] = []
    for stream, packets, groups in packet_sources:
        for packet in packets:
            context = step4.visual_assignment_context_for_packet(packet=packet, packets=packets, stream=stream, pipeline_mode=pipeline_mode, rows=rows, groups=groups)
            if context and context.get("assets"):
                contexts.append(context)
    out_path = run_dir / "visual_asset_assignment.json"
    write_json(out_path.with_name(out_path.stem + "_input_compact.json"), {"run_id": run_id, "mode": pipeline_mode, "pages": contexts})
    requests_list: list[dict[str, Any]] = []
    context_by_custom: dict[str, dict[str, Any]] = {}
    for index, context in enumerate(contexts, start=1):
        messages = step4.build_visual_asset_assignment_messages(run_id, pipeline_mode, context, "tool_calling")
        body = tool_body(args.model, messages, "submit_step4_visual_assets", "提交 Step4 单页视觉资产归属结果。", step4.VISUAL_ASSIGNMENT_TOOL_SCHEMA, temperature=0.0, enable_thinking=False)
        custom_id = f"step4-page{index:03d}"
        requests_list.append({"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions", "body": body})
        context_by_custom[custom_id] = context
    requests_path = args.output_root / "batch_jobs" / "step4_requests.jsonl"
    write_batch_requests(requests_path, requests_list)
    started = time.monotonic()
    final, output_path = run_batch(client, requests_path, args.output_root / "batch_jobs", "step4", f"{run_id} Step4", args.poll_seconds, args.batch_timeout, resume_batch_id=args.resume_step4_batch_id)
    rows_by_custom = read_batch_output(output_path)
    pages: list[dict[str, Any]] = []
    for custom_id, context in context_by_custom.items():
        raw = extract_tool_arguments(rows_by_custom[custom_id], "submit_step4_visual_assets")
        parsed = json.loads(raw)
        normalized = step4.normalize_visual_assignment_page(parsed, context)
        pages.append({"stream": context.get("stream"), "page": context.get("page"), "model": args.model, "structured_output": "tool_calling", "llm_elapsed_seconds": 0.0, "assets": normalized["assets"], "risks": normalized["risks"], "raw_response": raw})
    all_assets = [asset for page in pages for asset in page.get("assets") or []]
    all_risks = [risk for page in pages for risk in page.get("risks") or []]
    review = {
        "run_id": run_id,
        "model": args.model,
        "worker_models": [args.model],
        "worker_partitions": [],
        "mode": pipeline_mode,
        "pages": sorted(pages, key=lambda item: (str(item.get("stream") or ""), int(item.get("page") or 0))),
        "summary": {
            "asset_count": len(all_assets),
            "assigned_count": sum(1 for asset in all_assets if asset.get("question_no") is not None and str(asset.get("role") or "") not in {"noise", "uncertain"}),
            "noise_count": sum(1 for asset in all_assets if asset.get("is_noise")),
            "uncertain_count": sum(1 for asset in all_assets if asset.get("uncertain")),
            "risk_count": len(all_risks),
            "elapsed_seconds_sum": 0.0,
            "elapsed_seconds_max": 0.0,
            "batch_wall_seconds": round(time.monotonic() - started, 3),
            "batch": final,
        },
    }
    write_json(out_path, review)
    alignment = read_json(run_dir / "qa_alignment.json", {})
    rows = alignment.get("qa_alignment") or []
    if isinstance(rows, list):
        step4.apply_visual_asset_assignment_to_rows(rows, review, question_blocks, answer_blocks)
        step4.strip_transient_row_context(rows)
        alignment["qa_alignment"] = rows
    alignment["visual_asset_assignment_summary"] = review["summary"]
    alignment["asset_match_risk_count"] = int(review["summary"].get("risk_count") or 0)
    write_json(run_dir / "qa_alignment.json", alignment)
    if summary_path.exists():
        pipeline_summary.update({"visual_asset_assignment_elapsed_seconds_sum": 0.0, "visual_asset_assignment_elapsed_seconds_max": 0.0, "visual_asset_assignment_asset_count": review["summary"].get("asset_count"), "asset_match_risk_count": int(review["summary"].get("risk_count") or 0)})
        write_json(summary_path, pipeline_summary)
    step4.sync_step4_assets_to_question_bank(run_id=run_id, runs_root=runs_root, qb_root=qb_root)
    return review["summary"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Step3/Step3.5/Step4 through Bailian file Batch API.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.6-flash")
    parser.add_argument("--no-ocr-input", action="store_true", default=True)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--batch-timeout", type=int, default=7200)
    parser.add_argument("--steps", default="step3,step35,step4", help="Comma-separated subset: step3,step35,step4")
    parser.add_argument("--resume-step3-batch-id", default="", help="Poll and process an already-created Step3 batch without uploading a duplicate job.")
    parser.add_argument("--resume-step35-batch-id", default="", help="Poll and process an already-created Step3.5 batch without uploading a duplicate job.")
    parser.add_argument("--resume-step4-batch-id", default="", help="Poll and process an already-created Step4 batch without uploading a duplicate job.")
    args = parser.parse_args()
    args.runs_root = args.runs_root.resolve()
    args.output_root = args.output_root.resolve()
    args.output_root.mkdir(parents=True, exist_ok=True)
    client = BailianBatchClient(load_bailian_token())
    steps = {item.strip() for item in str(args.steps).split(",") if item.strip()}
    started = time.monotonic()
    step3_summary = run_step3_batch(args, client) if "step3" in steps else read_json(args.output_root / "question_bank" / args.run_id / "summary.json", {})
    step35_summary = run_step35_batch(args, client) if "step35" in steps else read_json(args.output_root / "step3_5_full_latex_normalize" / args.run_id / "summary.json", {})
    step4_summary = run_step4_batch(args, client) if "step4" in steps else read_json(args.runs_root / f"{args.run_id}_raw_units" / "visual_asset_assignment.json", {}).get("summary", {})
    final_summary = {"run_id": args.run_id, "model": args.model, "total_wall_seconds": round(time.monotonic() - started, 3), "step3": step3_summary, "step3_5": step35_summary, "step4": step4_summary}
    write_json(args.output_root / "file_batch_steps_summary.json", final_summary)
    print(json.dumps(final_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
