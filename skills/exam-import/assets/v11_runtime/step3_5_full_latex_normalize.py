#!/usr/bin/env python3
"""Full-record Step3.5 LaTeX and placeholder normalization.

This variant sends every selected Step3 record to a formatter model. The model
must return the complete record with normalized LaTeX and placeholders. Results
are still protected by the existing audit and content-fingerprint gates before
write-back.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from common_io import read_json, write_json, write_jsonl, write_text
from common_llm import load_token, post_chat_completion
from common_token_budget import DEFAULT_OUTPUT_RESERVE_TOKENS, configure_token_budget_env
from step3_5_question_json_audit_fix import (
    STEP3_RECORD_TOOL_SCHEMA,
    audit_record,
    content_fingerprint,
    field_set,
    html_entity_finding_count,
    load_question_bank,
    load_records,
    multi_model_name,
    normalize_worker_models,
)
from step3_schema import STRICT_JSON_SCHEMA as JSON_SCHEMA
from step3_schema import StrictStep3Record as Step3Record


WORKTREE_ROOT = Path(__file__).resolve().parent
DEFAULT_QB_ROOT = WORKTREE_ROOT / "runs_question_bank"
DEFAULT_OUTPUT_ROOT = WORKTREE_ROOT / "reviews_step3_5_full_latex_normalize"
DEFAULT_MODEL = "doubao-seed-2-0-pro-260215"

SYSTEM_PROMPT = """\
/no_think

你是数学题库的专业 LaTeX 与占位符规范化器。
本次任务必须调用工具 submit_step3_5_record 返回完整单题记录。
不得在普通正文中输出 JSON、Markdown、解释或分析过程。

你必须完整审计输入记录中的所有文字字段：
- stem_latex
- options_latex.A/B/C/D
- answer_latex
- analysis_latex
- rubric_latex

你的目标是重新输出完整、规范、可 MathJax 渲染的 LaTeX 文本，以及合适的 <blank>、<choice_blank> 占位符。
"""

USER_PROMPT = """\
请对下面这道题做完整文字审计，并输出完整规范化后的单题记录。

硬性边界：
- 不得解题，不得补充解析，不得改变题意、答案、题号、题型、选项键或字段结构。
- 不得删除原有有效文字、数字、公式、选项、答案或解析步骤。
- 可以且应该重排 LaTeX 定界、空格和占位符位置，使文本专业、稳定、可渲染。
- issues 字段保留原有含义；除非只是删除已经明显不再成立的格式类提示，否则不要新增业务判断。
- 不得改变原有数学符号写法本身。例如原文是 $N^*$ 就保持 $N^*$，不得改成 $\\mathbf{N}^*$ 或 $\\mathbb{N}^*$；原文是 $\\mathbf{N}^*$ 也不得改成 $N^*$。

LaTeX 规范：
- 所有数学表达式必须放在 $...$ 中；展示公式可放在 $$...$$ 中。
- 中文说明文字一般放在数学环境外；数学环境内需要中文连接词时，用 \\text{...}。
- 不要把完整中文句子整段放进数学环境，除非它本来就是展示公式的一部分。
- 已经正确的数学表达式不要重复包裹。
- 数学集合、数列、绝对值、区间、上下标、分式、根式、向量、三角函数、对数等都必须是合法 LaTeX。
- 配平 \\left 与 \\right；如果没有必要，优先去掉多余的 \\left 或 \\right。
- HTML 实体必须转为等价字符，例如 &gt; 改为 >，&lt; 改为 <，&amp; 改为 &。

<blank> 规范：
- <blank> 表示填空题的作答位置，必须保留。
- <blank> 不应放在 $...$ 内。
- 如果填空位置紧接公式末尾，应写成 `$公式 = $ <blank>`，或者 `$公式$ <blank>`。
- 不要输出 `$公式 = $<blank>`、`$公式 = <blank>$`、`=<blank>` 这类不稳定形式。
- 如果原文用空括号、横线或空白表示填空，应统一为 <blank>。
- 同一道填空题中不要凭空新增多个 <blank>。

<choice_blank> 规范：
- 选择题题干必须且只能有一个 <choice_blank>。
- 如果原文作答位是 `（）`、`( )`、空括号或类似选择题空位，应统一为 <choice_blank>。
- <choice_blank> 不应放在 $...$ 内。
- 不要把 <choice_blank> 放到选项内；它属于题干作答位置。

字段格式：
- stem_latex、answer_latex、analysis_latex、rubric_latex 都是字符串数组。
- options_latex 必须包含 A、B、C、D 四个键，每个键都是字符串数组。
- 每个数组项必须是单行字符串，不得包含换行。
- 不要输出空字符串数组项。
- 保留 <image01>、<table01>、<figure01> 等资产占位符。
- 保留 HTML 表格结构；只规范表格单元格内的数学文本。

输入中还包含脚本审计发现，用来提醒重点问题；即使没有发现，也要完整审计所有文字字段。

输入数据：
"""


def call_normalizer(
    record: dict[str, Any],
    findings: list[dict[str, Any]],
    model: str,
    timeout: int,
    token: str,
    enable_thinking: bool,
    structured_output: str,
) -> tuple[dict[str, Any], str, float]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT
                + json.dumps(
                    {
                        "record": record,
                        "audit_findings": findings,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        "temperature": 0.0,
        "top_p": 0.8,
        "enable_thinking": enable_thinking,
    }
    if structured_output == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "Step3Record",
                "schema": JSON_SCHEMA,
                "strict": True,
            },
        }
    elif structured_output == "tool_calling":
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "submit_step3_5_record",
                    "description": "提交完整规范化后的 Step3.5 单题记录。",
                    "parameters": STEP3_RECORD_TOOL_SCHEMA,
                },
            }
        ]
        body["tool_choice"] = {"type": "function", "function": {"name": "submit_step3_5_record"}}
    else:
        raise ValueError(f"Unsupported structured_output: {structured_output}")

    started = time.monotonic()
    payload = post_chat_completion(body, timeout=timeout, token=token)
    if structured_output == "tool_calling":
        try:
            tool_call = payload["choices"][0]["message"]["tool_calls"][0]
            actual_name = tool_call["function"].get("name")
            if actual_name != "submit_step3_5_record":
                raise RuntimeError(f"Step3.5 full normalizer called unexpected tool {actual_name!r}")
            raw = tool_call["function"]["arguments"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Step3.5 full normalizer returned no tool call: {json.dumps(payload, ensure_ascii=False)[:1200]}") from exc
    else:
        raw = payload["choices"][0]["message"]["content"]
    parsed = json.loads(raw)
    validated = Step3Record.model_validate(parsed).model_dump(mode="json")
    return validated, raw, time.monotonic() - started


def result_is_applicable(result: dict[str, Any]) -> tuple[bool, list[str]]:
    if not str(result.get("status") or "").startswith("normalized"):
        return False, ["not_normalized"]
    return True, []


def merge_normalized_fields(
    base_records: list[dict[str, Any]],
    normalized_by_qno: dict[int, dict[str, Any]],
    results: list[dict[str, Any]],
    fields: set[str],
) -> tuple[list[dict[str, Any]], list[int], list[dict[str, Any]]]:
    result_by_qno = {int(item.get("question_no") or 0): item for item in results}
    merged: list[dict[str, Any]] = []
    applied: list[int] = []
    rejected: list[dict[str, Any]] = []
    for record in base_records:
        qno = int(record.get("question_no") or 0)
        fixed = normalized_by_qno.get(qno)
        result = result_by_qno.get(qno)
        if not fixed or not result:
            merged.append(record)
            continue
        ok, reasons = result_is_applicable(result)
        if not ok:
            if reasons != ["not_normalized"]:
                rejected.append({"question_no": qno, "reasons": reasons})
            merged.append(record)
            continue
        out = dict(record)
        changed = False
        for field in sorted(fields):
            if field in fixed and out.get(field) != fixed.get(field):
                out[field] = fixed.get(field)
                changed = True
        if changed:
            applied.append(qno)
        merged.append(out)
    return merged, applied, rejected


def write_back_question_bank(run_dir: Path, records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    qb_path = run_dir / "question_bank.json"
    backup_path = run_dir / "question_bank.before_step3_5_full_latex_normalize.json"
    if qb_path.exists() and not backup_path.exists():
        backup_path.write_text(qb_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    write_json(qb_path, records)
    write_jsonl(run_dir / "question_bank.jsonl", records)
    write_json(run_dir / "step3_5_full_latex_normalize_summary.json", summary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize every selected Step3 record with a full LaTeX/placeholder Step3.5 pass.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--qb-root", type=Path, default=DEFAULT_QB_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--worker-model", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--tpm-limit", type=int, default=None)
    parser.add_argument("--token-estimator-model", default=None)
    parser.add_argument("--image-token-mode", choices=["auto", "processor", "formula"], default="auto")
    parser.add_argument("--tpm-output-reserve", type=int, default=DEFAULT_OUTPUT_RESERVE_TOKENS)
    parser.add_argument("--token-budget-log", type=Path, default=None)
    parser.add_argument("--token-budget-verbose", action="store_true")
    parser.add_argument("--question-no", action="append", type=int)
    parser.add_argument("--source-stage", choices=["validated", "question_bank"], default="question_bank")
    parser.add_argument("--fields", default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--structured-output", choices=["json_schema", "tool_calling"], default="tool_calling")
    parser.add_argument("--merge-base-qb-root", type=Path, default=None)
    parser.add_argument("--write-back", action="store_true")
    args = parser.parse_args()

    configure_token_budget_env(
        os.environ,
        tpm_limit=args.tpm_limit,
        estimator_model=args.token_estimator_model or args.model,
        image_token_mode=args.image_token_mode,
        output_reserve_tokens=args.tpm_output_reserve,
        verbose=args.token_budget_verbose,
        log_path=args.token_budget_log,
    )

    run_dir = args.qb_root / args.run_id
    out_dir = args.output_root / args.run_id
    fields = field_set(args.fields)
    worker_models = normalize_worker_models(args.model, args.worker_model)
    question_numbers = set(args.question_no or [])
    records = load_records(run_dir, question_numbers or None, args.source_stage)
    before_findings_by_qno = {int(record["question_no"]): audit_record(record, fields) for record in records}
    model_by_qno = {
        int(record["question_no"]): worker_models[index % len(worker_models)]
        for index, record in enumerate(records)
    }

    write_json(
        out_dir / "audit_before.json",
        {
            "run_id": args.run_id,
            "source_stage": args.source_stage,
            "fields": sorted(fields),
            "finding_count": sum(len(items) for items in before_findings_by_qno.values()),
            "question_numbers": [int(record["question_no"]) for record in records],
            "findings": [item for qno in sorted(before_findings_by_qno) for item in before_findings_by_qno[qno]],
        },
    )

    results: list[dict[str, Any]] = []
    normalized_by_qno: dict[int, dict[str, Any]] = {}
    token = None if args.dry_run or not records else load_token()

    def run_record(record: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
        qno = int(record["question_no"])
        findings = before_findings_by_qno.get(qno) or []
        per_dir = out_dir / "per_question"
        assigned_model = model_by_qno[qno]
        write_json(per_dir / f"q{qno:02d}_before.json", record)
        write_json(per_dir / f"q{qno:02d}_findings_before.json", findings)
        if args.dry_run:
            return qno, record, {
                "question_no": qno,
                "status": "selected_dry_run",
                "before_finding_count": len(findings),
                "model": assigned_model,
            }
        try:
            assert token is not None
            fixed, raw, elapsed = call_normalizer(
                record,
                findings,
                assigned_model,
                args.timeout,
                token,
                args.enable_thinking,
                args.structured_output,
            )
            if int(fixed.get("question_no") or 0) != qno:
                raise ValueError(f"question_no mismatch: expected {qno}, got {fixed.get('question_no')}")
            before_fp = content_fingerprint(record, fields)
            after_fp = content_fingerprint(fixed, fields)
            write_text(per_dir / f"q{qno:02d}_raw_after.json.txt", raw)
            write_json(per_dir / f"q{qno:02d}_normalized.json", fixed)
            return qno, fixed, {
                "question_no": qno,
                "status": "normalized",
                "elapsed_seconds": round(elapsed, 3),
                "before_finding_count": len(findings),
                "before_html_entity_finding_count": html_entity_finding_count(findings),
                "changed_content_fingerprint": before_fp != after_fp,
                "model": assigned_model,
                "structured_output": args.structured_output,
            }
        except (ValidationError, json.JSONDecodeError, Exception) as exc:  # noqa: BLE001
            return qno, record, {
                "question_no": qno,
                "status": "error",
                "before_finding_count": len(findings),
                "error": f"{type(exc).__name__}: {exc}",
                "model": assigned_model,
                "structured_output": args.structured_output,
            }

    if records:
        max_workers = args.max_workers or len(worker_models)
        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(records)))) as pool:
            futures = {pool.submit(run_record, record): int(record["question_no"]) for record in records}
            for future in as_completed(futures):
                qno, fixed, result = future.result()
                normalized_by_qno[qno] = fixed
                results.append(result)

    fixed_records = [normalized_by_qno.get(int(record["question_no"]), record) for record in records]
    merge_base_root = args.merge_base_qb_root or args.qb_root
    merge_run_dir = merge_base_root / args.run_id
    base_records = load_question_bank(merge_run_dir) if (merge_run_dir / "question_bank.json").exists() else records
    merged_records, applied_qnos, rejected_qnos = merge_normalized_fields(base_records, normalized_by_qno, results, fields)
    summary = {
        "run_id": args.run_id,
        "source_stage": args.source_stage,
        "model": multi_model_name(worker_models),
        "worker_models": worker_models,
        "model_assignment": {str(qno): model_by_qno[qno] for qno in sorted(model_by_qno)},
        "enable_thinking": args.enable_thinking,
        "structured_output": args.structured_output,
        "dry_run": args.dry_run,
        "field_scope": sorted(fields),
        "question_count": len(records),
        "before_finding_count": sum(len(items) for items in before_findings_by_qno.values()),
        "normalized_count": sum(1 for item in results if str(item.get("status") or "").startswith("normalized")),
        "error_count": sum(1 for item in results if str(item.get("status") or "") == "error"),
        "applied_question_numbers": applied_qnos,
        "rejected_question_numbers": rejected_qnos,
        "write_back": bool(args.write_back and not args.dry_run),
        "results": sorted(results, key=lambda item: int(item.get("question_no") or 0)),
    }
    write_json(out_dir / "normalized_question_bank.json", fixed_records)
    write_json(out_dir / "merged_question_bank.json", merged_records)
    write_jsonl(out_dir / "merged_question_bank.jsonl", merged_records)
    write_json(out_dir / "summary.json", summary)
    if args.write_back and not args.dry_run:
        write_back_question_bank(merge_run_dir, merged_records, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
