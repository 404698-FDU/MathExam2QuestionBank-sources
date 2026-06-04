from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from exam_import.core.io import read_json, write_json, write_jsonl
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
from exam_import.schemas.common import ValidationError
from exam_import.schemas.question_record import QuestionRecord, load_question_record


FORMAT_ENTITY_PATTERNS = ("&gt;", "&lt;", "&amp;", "&nbsp;")
INLINE_MATH_CONNECTOR_PATTERN = re.compile(
    r"\$[^$\n]+\$\s*(且|或|并且|同时|并|与)\s*\$[^$\n]+\$"
)
OPTIONS_TAG_PATTERN = re.compile(r'<options no="([^"]+)">')


@dataclass(frozen=True)
class AuditFinding:
    field: str
    path: str
    reason: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "path": self.path,
            "reason": self.reason,
            "text": self.text,
        }


def audit_record(record: QuestionRecord) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for index, value in enumerate(record.stem_markdown):
        findings.extend(_audit_text_segment("stem_markdown", f"stem_markdown[{index}]", value))
    for group_index, group in enumerate(record.options_markdown):
        for option_index, option in enumerate(group.options):
            for item_index, value in enumerate(option.content_markdown):
                findings.extend(
                    _audit_text_segment(
                        "options_markdown",
                        f"options_markdown[{group_index}].options[{option_index}].content_markdown[{item_index}]",
                        value,
                    )
                )
    for index, value in enumerate(record.answer_markdown):
        findings.extend(_audit_text_segment("answer_markdown", f"answer_markdown[{index}]", value))
    for index, value in enumerate(record.analysis_markdown):
        findings.extend(_audit_text_segment("analysis_markdown", f"analysis_markdown[{index}]", value))
    findings.extend(_audit_options_group_references(record))
    return findings


def _audit_text_segment(field: str, path: str, text: str) -> list[AuditFinding]:
    results: list[AuditFinding] = []
    for entity in FORMAT_ENTITY_PATTERNS:
        if entity in text:
            results.append(AuditFinding(field=field, path=path, reason="html_entity_present", text=text))
            break
    if "\n" in text:
        results.append(AuditFinding(field=field, path=path, reason="embedded_newline", text=text))
    if INLINE_MATH_CONNECTOR_PATTERN.search(text):
        results.append(
            AuditFinding(
                field=field,
                path=path,
                reason="math_condition_split_across_text_connector",
                text=text,
            )
        )
    return results


def _audit_options_group_references(record: QuestionRecord) -> list[AuditFinding]:
    stem_group_nos: list[str] = []
    for segment in record.stem_markdown:
        stem_group_nos.extend(OPTIONS_TAG_PATTERN.findall(segment))
    option_group_nos = [group.no for group in record.options_markdown]
    if stem_group_nos == option_group_nos and len(stem_group_nos) == len(set(stem_group_nos)):
        return []
    return [
        AuditFinding(
            field="options_markdown",
            path="options_markdown",
            reason="options_group_reference_mismatch",
            text=(
                f"stem_options={stem_group_nos}; "
                f"options_markdown={option_group_nos}"
            ),
        )
    ]


def build_step35_messages(
    *,
    record: QuestionRecord,
    audit_findings: list[AuditFinding],
    prompt_loader: PromptLoader,
    prompt_ref: str = "step35_latex_audit",
) -> list[dict[str, Any]]:
    system_prompt = prompt_loader.load_system_text(prompt_ref)
    user_prompt = prompt_loader.render_user_text(prompt_ref).replace(
        "{record_and_audit_findings_json}",
        _record_and_findings_json(record, audit_findings),
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def call_step35_record(
    *,
    record: QuestionRecord,
    call_spec_path: Path,
    audit_findings: list[AuditFinding] | None = None,
    client: LLMClient | None = None,
    prompt_loader: PromptLoader | None = None,
    env: dict[str, str] | None = None,
) -> QuestionRecord:
    prompt_loader = prompt_loader or PromptLoader()
    findings = audit_findings if audit_findings is not None else audit_record(record)
    resolved = load_and_resolve_call_spec(call_spec_path, prompt_loader=prompt_loader)
    messages = build_step35_messages(
        record=record,
        audit_findings=findings,
        prompt_loader=prompt_loader,
        prompt_ref=resolved.call_spec.prompt,
    )
    tool_schema = resolve_tool_schema(resolved.call_spec.tool_schema or "submit_step3_5_record")
    payload = build_request_payload(
        resolved,
        messages=messages,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": resolved.call_spec.tool_name,
                    "description": "提交 Step3.5 full 完整规范化后的单题题库 JSON 记录。",
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
    return parse_step35_response(response, expected_tool_name=resolved.call_spec.tool_name, expected_question_no=record.question_no)


def parse_step35_response(
    response: LLMResponse,
    *,
    expected_tool_name: str,
    expected_question_no: int,
) -> QuestionRecord:
    payload = parse_tool_call_arguments(response, expected_tool_name=expected_tool_name)
    record = QuestionRecord.from_dict(payload)
    if record.question_no != expected_question_no:
        raise ValidationError(
            f"Step3.5 returned question_no={record.question_no}, expected {expected_question_no}"
        )
    return record


def write_step35_outputs(
    *,
    run_context: RunContext,
    normalized_records: list[QuestionRecord],
    before_findings: dict[int, list[AuditFinding]] | None = None,
    after_findings: dict[int, list[AuditFinding]] | None = None,
    write_back: bool = False,
) -> dict[str, Any]:
    out_dir = run_context.review_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized_payloads = [item.to_dict() for item in sorted(normalized_records, key=lambda item: item.question_no)]
    write_json(out_dir / "normalized_question_bank.json", normalized_payloads)
    write_json(out_dir / "merged_question_bank.json", normalized_payloads)
    write_jsonl(out_dir / "merged_question_bank.jsonl", normalized_payloads)
    summary = {
        "run_id": run_context.run_id,
        "question_count": len(normalized_payloads),
        "normalized_count": len(normalized_payloads),
        "before_finding_count": sum(len(values) for values in (before_findings or {}).values()),
        "after_finding_count": sum(len(values) for values in (after_findings or {}).values()),
        "error_count": 0,
    }
    write_json(out_dir / "summary.json", summary)
    if write_back:
        qb_path = run_context.question_bank_dir / "question_bank.json"
        if qb_path.exists():
            write_json(
                run_context.question_bank_dir / "question_bank.before_step3_5_latex_audit.json",
                read_json(qb_path, []),
            )
        write_question_bank(run_context.question_bank_dir, normalized_records)
    return summary


def _record_and_findings_json(record: QuestionRecord, findings: list[AuditFinding]) -> str:
    import json

    payload = {
        "record": record.to_dict(),
        "audit_findings": [item.to_dict() for item in findings],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
