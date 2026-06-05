from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from exam_import.core.io import data_uri, write_json
from exam_import.core.question_bank import write_question_bank
from exam_import.core.run_context import RunContext
from exam_import.llm import (
    load_and_resolve_call_spec,
    parse_tool_call_arguments,
    resolve_tool_schema,
)
from exam_import.llm.client import LLMClient, LLMResponse
from exam_import.llm.request_builder import build_request_payload
from exam_import.prompts.loader import PromptLoader
from exam_import.schemas.common import IssueRecord
from exam_import.schemas.common import ValidationError
from exam_import.schemas.qa_alignment import QAAlignmentDocument
from exam_import.schemas.question_record import QuestionRecord, ensure_options_placeholders_in_payload


ASSET_PLACEHOLDER_RE = re.compile(r"<(img|table|chart)\s+src=\"([^\"]+)\">")


@dataclass(frozen=True)
class Step3Job:
    question_no: int
    question_image_paths: list[Path]
    answer_image_paths: list[Path]
    allowed_asset_labels: list[str]


def build_step3_messages(
    *,
    job: Step3Job,
    prompt_loader: PromptLoader,
    prompt_ref: str = "step3_question_json",
) -> list[dict[str, Any]]:
    system_prompt = prompt_loader.load_system_text(prompt_ref)
    user_prompt = prompt_loader.render_user_text(
        prompt_ref,
        variables={
            "question_no": job.question_no,
            "allowed_asset_labels_json": json.dumps(job.allowed_asset_labels, ensure_ascii=False),
        },
    )
    content: list[dict[str, Any]] = []
    for index, path in enumerate(job.question_image_paths, start=1):
        content.append({"type": "text", "text": f"第 {job.question_no} 题题面裁剪图 {index}："})
        content.append({"type": "image_url", "image_url": {"url": data_uri(path)}})
    for index, path in enumerate(job.answer_image_paths, start=1):
        content.append({"type": "text", "text": f"第 {job.question_no} 题答案/解析裁剪图 {index}："})
        content.append({"type": "image_url", "image_url": {"url": data_uri(path)}})
    content.append({"type": "text", "text": user_prompt})
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def call_step3_job(
    *,
    job: Step3Job,
    call_spec_path: Path,
    client: LLMClient | None = None,
    prompt_loader: PromptLoader | None = None,
    env: dict[str, str] | None = None,
) -> QuestionRecord:
    prompt_loader = prompt_loader or PromptLoader()
    resolved = load_and_resolve_call_spec(call_spec_path, prompt_loader=prompt_loader)
    messages = build_step3_messages(
        job=job,
        prompt_loader=prompt_loader,
        prompt_ref=resolved.call_spec.prompt,
    )
    tool_schema = resolve_tool_schema(resolved.call_spec.tool_schema or "QuestionRecord")
    payload = build_request_payload(
        resolved,
        messages=messages,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": resolved.call_spec.tool_name,
                    "description": "提交 Step3 单题题库 JSON 记录。",
                    "parameters": tool_schema,
                },
            }
        ],
        tool_choice={
            "type": "function",
            "function": {"name": resolved.call_spec.tool_name},
        },
    )
    client = client or LLMClient()
    response = client.send_chat(resolved, payload, env=env)
    return parse_step3_response(response, expected_tool_name=resolved.call_spec.tool_name, expected_question_no=job.question_no)


def parse_step3_response(
    response: LLMResponse,
    *,
    expected_tool_name: str,
    expected_question_no: int,
) -> QuestionRecord:
    payload = parse_tool_call_arguments(response, expected_tool_name=expected_tool_name)
    _drop_empty_latex_segments(payload)
    ensure_options_placeholders_in_payload(payload)
    record = QuestionRecord.from_dict(payload)
    if record.question_no != expected_question_no:
        raise ValidationError(
            f"Step3 returned question_no={record.question_no}, expected {expected_question_no}"
        )
    return record


def _drop_empty_latex_segments(payload: dict[str, Any]) -> None:
    for field_name in ("stem_latex", "answer_latex", "analysis_latex"):
        value = payload.get(field_name)
        if isinstance(value, list):
            payload[field_name] = [item for item in value if str(item).strip()]
    options = payload.get("options_latex")
    if not isinstance(options, list):
        return
    for group in options:
        if not isinstance(group, dict):
            continue
        for option in group.get("options") or []:
            if not isinstance(option, dict):
                continue
            content = option.get("content_latex")
            if isinstance(content, list):
                option["content_latex"] = [item for item in content if str(item).strip()]


def sanitize_step3_asset_placeholders(
    *,
    record: QuestionRecord,
    qa_alignment: QAAlignmentDocument,
) -> QuestionRecord:
    allowed_labels = set(allowed_asset_labels_for_question(record.question_no, qa_alignment))
    payload = record.to_dict()
    removed_labels: set[str] = set()

    for field_name in ("stem_latex", "answer_latex", "analysis_latex"):
        payload[field_name], removed = _filter_asset_placeholders(payload[field_name], allowed_labels)
        removed_labels.update(removed)

    for group in payload["options_latex"]:
        for option in group["options"]:
            option["content_latex"], removed = _filter_asset_placeholders(option["content_latex"], allowed_labels)
            removed_labels.update(removed)

    if removed_labels:
        payload["issues"].append(
            IssueRecord(
                type="content_missing",
                severity="warning",
                message="已移除模型生成但 Step2 未提供映射的资产占位标签：" + ", ".join(sorted(removed_labels)),
            ).to_dict()
        )
    return QuestionRecord.from_dict(payload)


def allowed_asset_labels_for_question(question_no: int, qa_alignment: QAAlignmentDocument) -> list[str]:
    for row in qa_alignment.qa_alignment:
        if row.question_no != question_no:
            continue
        labels = set(row.question.visual_labels)
        for item in row.answer.items:
            labels.update(item.labels)
        return sorted(label for label in labels if _looks_like_asset_label(label))
    return []


def _looks_like_asset_label(label: str) -> bool:
    return "-P" in label or "-T" in label or "-C" in label


def _filter_asset_placeholders(segments: list[str], allowed_labels: set[str]) -> tuple[list[str], set[str]]:
    removed_labels: set[str] = set()
    filtered: list[str] = []
    for segment in segments:
        matches = ASSET_PLACEHOLDER_RE.findall(segment)
        if matches and any(label not in allowed_labels for _, label in matches):
            for _, label in matches:
                if label not in allowed_labels:
                    removed_labels.add(label)
            stripped = ASSET_PLACEHOLDER_RE.sub(
                lambda match: match.group(0) if match.group(2) in allowed_labels else "",
                segment,
            ).strip()
            if stripped:
                filtered.append(stripped)
            continue
        filtered.append(segment)
    return filtered, removed_labels


def write_step3_outputs(
    *,
    run_context: RunContext,
    records: list[QuestionRecord],
    model_name: str,
    errors: list[dict[str, Any]] | None = None,
    elapsed_seconds: float | None = None,
    retry_count: int = 0,
    attempt_count: int | None = None,
    worker_count: int = 1,
) -> dict[str, Any]:
    per_question_dir = run_context.question_bank_dir / "per_question"
    per_question_dir.mkdir(parents=True, exist_ok=True)
    record_payloads = write_question_bank(run_context.question_bank_dir, records)
    for record in sorted(records, key=lambda item: item.question_no):
        write_json(per_question_dir / f"q{record.question_no:03d}.json", record.to_dict())
    summary = {
        "run_id": run_context.run_id,
        "alignment_mode": run_context.alignment_mode,
        "model": model_name,
        "question_count": len(record_payloads),
        "success_count": len(record_payloads),
        "error_count": len(errors or []),
        "record_schema_version": "image_only_question_standardization_v1",
        "question_numbers": [int(item["question_no"]) for item in record_payloads],
        "worker_count": worker_count,
        "retry_count": retry_count,
        "attempt_count": attempt_count if attempt_count is not None else len(record_payloads) + len(errors or []),
    }
    if elapsed_seconds is not None:
        summary["elapsed_seconds"] = elapsed_seconds
    if errors:
        write_json(run_context.question_bank_dir / "errors.json", errors)
    write_json(run_context.question_bank_dir / "summary.json", summary)
    return summary
