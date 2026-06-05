from __future__ import annotations

import copy
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
from exam_import.schemas.question_record import (
    QuestionRecord,
    ensure_options_placeholders_in_payload,
    load_question_record,
)


HTML_ENTITY_PATTERN = re.compile(r"&(?:gt|lt|amp|ge|le|nbsp|quot|apos|#\d+|#x[0-9A-Fa-f]+);")
INLINE_MATH_CONNECTOR_PATTERN = re.compile(
    r"\$[^$\n]+\$\s*(且|或|并且|同时|并|与)\s*\$[^$\n]+\$"
)
OPTIONS_TAG_PATTERN = re.compile(r'<options no="([^"]+)"\s*/?>')
MATH_COMMAND_PATTERN = re.compile(
    r"\\(?:frac|sqrt|vec|overrightarrow|cdot|times|leq|geq|neq|in|notin|"
    r"mathbb|mathbf|mathrm|text|sin|cos|tan|log|ln|alpha|beta|gamma|"
    r"pi|theta|Delta|left|right|mid|parallel|perp|angle|degree|circ|"
    r"sum|prod|int|lim)\b"
)
MATH_RELATION_PATTERN = re.compile(
    r"(?:[A-Za-z](?:_\{[^{}]+\})?|\d+(?:\.\d+)?|[)}])\s*"
    r"(?:[<>]=?|=|\\leq|\\geq|\\neq)\s*"
    r"(?:[A-Za-z](?:_\{[^{}]+\})?|\d+(?:\.\d+)?|\\[A-Za-z]+|[({])"
)
SUBSUP_PATTERN = re.compile(r"[A-Za-z0-9)}]\s*[_^]\s*(?:\{|[A-Za-z0-9])")
LEFT_TOKEN_PATTERN = re.compile(r"\\left\b")
RIGHT_TOKEN_PATTERN = re.compile(r"\\right\b")
ALLOWED_PLACEHOLDER_PATTERN = re.compile(
    r'<(?:blank|choice_blank|options\s+no="[^"]+"\s*/?|(?:img|table|chart)\s+src="[^"]+"\s*/?)>'
)
TAG_PATTERN = re.compile(r"</?[A-Za-z][^<>]*>")
BLANK_PATTERN = re.compile(r"<blank>")
CHOICE_BLANK_PATTERN = re.compile(r"<choice_blank>")
CHOICE_BRACKET_PATTERN = re.compile(r"(?:（\s*）|\(\s*\))")
ASSET_PLACEHOLDER_PATTERN = re.compile(r'<(?:img|table|chart)\s+src="[^"]+"\s*/?>')
PATCH_TEXT_PATH_PATTERN = re.compile(r"^(stem_latex|answer_latex|analysis_latex)\[(\d+)\]$")
PATCH_OPTION_PATH_PATTERN = re.compile(
    r"^options_latex\[(\d+)\]\.options\[(\d+)\]\.content_latex\[(\d+)\]$"
)


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


@dataclass(frozen=True)
class Step35PatchEdit:
    path: str
    op: str
    expected_old_json: str
    expected_old: str
    new: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "op": self.op,
            "expected_old_json": self.expected_old_json,
            "new": self.new,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], index: int) -> "Step35PatchEdit":
        path = payload.get("path")
        op = payload.get("op")
        expected_old_json = payload.get("expected_old_json")
        new = payload.get("new")
        reason = payload.get("reason")
        if not isinstance(path, str) or not path:
            raise ValidationError(f"Step3.5 patch edits[{index}].path must be a non-empty string")
        if op != "replace":
            raise ValidationError(f"Step3.5 patch edits[{index}].op must be replace")
        if not isinstance(expected_old_json, str) or not expected_old_json:
            raise ValidationError(f"Step3.5 patch edits[{index}].expected_old_json must be a non-empty string")
        try:
            import json

            expected_old = json.loads(expected_old_json)
        except Exception as exc:
            raise ValidationError(
                f"Step3.5 patch edits[{index}].expected_old_json must decode as a JSON string"
            ) from exc
        if not isinstance(expected_old, str):
            raise ValidationError(f"Step3.5 patch edits[{index}].expected_old_json must decode as a string")
        if not isinstance(new, str) or not new:
            raise ValidationError(f"Step3.5 patch edits[{index}].new must be a non-empty string")
        if not isinstance(reason, str) or not reason:
            raise ValidationError(f"Step3.5 patch edits[{index}].reason must be a non-empty string")
        return cls(
            path=path,
            op=op,
            expected_old_json=expected_old_json,
            expected_old=expected_old,
            new=new,
            reason=reason,
        )


@dataclass(frozen=True)
class Step35PatchReview:
    schema_version: str
    question_no: int
    edits: list[Step35PatchEdit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "question_no": self.question_no,
            "edits": [item.to_dict() for item in self.edits],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Step35PatchReview":
        schema_version = payload.get("schema_version")
        if schema_version != "step35_latex_patch_v1":
            raise ValidationError(
                f"Step3.5 patch schema_version must be step35_latex_patch_v1, got {schema_version!r}"
            )
        question_no = payload.get("question_no")
        if not isinstance(question_no, int) or question_no < 1:
            raise ValidationError("Step3.5 patch question_no must be a positive integer")
        edits_payload = payload.get("edits")
        if not isinstance(edits_payload, list):
            raise ValidationError("Step3.5 patch edits must be an array")
        edits: list[Step35PatchEdit] = []
        for index, item in enumerate(edits_payload):
            if not isinstance(item, dict):
                raise ValidationError(f"Step3.5 patch edits[{index}] must be an object")
            edits.append(Step35PatchEdit.from_dict(item, index))
        return cls(schema_version=schema_version, question_no=question_no, edits=edits)


def audit_record(record: QuestionRecord) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for index, value in enumerate(record.stem_latex):
        findings.extend(_audit_text_segment("stem_latex", f"stem_latex[{index}]", value))
    for group_index, group in enumerate(record.options_latex):
        for option_index, option in enumerate(group.options):
            for item_index, value in enumerate(option.content_latex):
                findings.extend(
                    _audit_text_segment(
                        "options_latex",
                        f"options_latex[{group_index}].options[{option_index}].content_latex[{item_index}]",
                        value,
                    )
                )
    for index, value in enumerate(record.answer_latex):
        findings.extend(_audit_text_segment("answer_latex", f"answer_latex[{index}]", value))
    for index, value in enumerate(record.analysis_latex):
        findings.extend(_audit_text_segment("analysis_latex", f"analysis_latex[{index}]", value))
    findings.extend(_audit_options_group_references(record))
    findings.extend(_audit_choice_blank_record(record))
    return findings


def _audit_text_segment(field: str, path: str, text: str) -> list[AuditFinding]:
    results: list[AuditFinding] = []
    if HTML_ENTITY_PATTERN.search(text):
        results.append(AuditFinding(field=field, path=path, reason="html_entity_present", text=text))
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
    for tag in TAG_PATTERN.findall(text):
        if not ALLOWED_PLACEHOLDER_PATTERN.fullmatch(tag):
            results.append(AuditFinding(field=field, path=path, reason="unknown_placeholder_tag", text=text))
            break
    for is_math, segment in _split_math_segments(text):
        if is_math:
            if len(LEFT_TOKEN_PATTERN.findall(segment)) != len(RIGHT_TOKEN_PATTERN.findall(segment)):
                results.append(AuditFinding(field=field, path=path, reason="left_right_count_mismatch", text=text))
            if BLANK_PATTERN.search(segment):
                results.append(AuditFinding(field=field, path=path, reason="blank_inside_math", text=text))
            if CHOICE_BLANK_PATTERN.search(segment):
                results.append(AuditFinding(field=field, path=path, reason="choice_blank_inside_math", text=text))
            if ASSET_PLACEHOLDER_PATTERN.search(segment):
                results.append(AuditFinding(field=field, path=path, reason="asset_placeholder_inside_math", text=text))
            continue

        outside = TAG_PATTERN.sub(" ", segment)
        if MATH_COMMAND_PATTERN.search(outside):
            results.append(AuditFinding(field=field, path=path, reason="latex_command_outside_math", text=text))
        if MATH_RELATION_PATTERN.search(outside):
            results.append(AuditFinding(field=field, path=path, reason="math_relation_outside_math", text=text))
        if SUBSUP_PATTERN.search(outside):
            results.append(AuditFinding(field=field, path=path, reason="subsup_outside_math", text=text))
    return results


def _audit_options_group_references(record: QuestionRecord) -> list[AuditFinding]:
    stem_group_nos: list[str] = []
    for segment in record.stem_latex:
        stem_group_nos.extend(OPTIONS_TAG_PATTERN.findall(segment))
    option_group_nos = [group.no for group in record.options_latex]
    if not stem_group_nos and not option_group_nos:
        return []
    if stem_group_nos == option_group_nos and len(stem_group_nos) == len(set(stem_group_nos)):
        return []
    return [
        AuditFinding(
            field="options_latex",
            path="options_latex",
            reason="options_group_reference_mismatch",
            text=(
                f"stem_options={stem_group_nos}; "
                f"options_latex={option_group_nos}"
            ),
        )
    ]


def _audit_choice_blank_record(record: QuestionRecord) -> list[AuditFinding]:
    if not record.options_latex:
        return []

    findings: list[AuditFinding] = []
    stem_text = "\n".join(record.stem_latex)
    choice_blank_count = sum(len(CHOICE_BLANK_PATTERN.findall(segment)) for segment in record.stem_latex)
    raw_choice_blank_count = sum(
        len(CHOICE_BRACKET_PATTERN.findall(segment))
        for stem_segment in record.stem_latex
        for is_math, segment in _split_math_segments(stem_segment)
        if not is_math
    )

    if choice_blank_count == 0:
        findings.append(
            AuditFinding(
                field="stem_latex",
                path="stem_latex",
                reason="missing_choice_blank",
                text=stem_text,
            )
        )
    elif choice_blank_count > 1:
        findings.append(
            AuditFinding(
                field="stem_latex",
                path="stem_latex",
                reason="duplicate_choice_blank",
                text=stem_text,
            )
        )

    if raw_choice_blank_count:
        findings.append(
            AuditFinding(
                field="stem_latex",
                path="stem_latex",
                reason="raw_choice_blank_not_normalized",
                text=stem_text,
            )
        )

    for group_index, group in enumerate(record.options_latex):
        for option_index, option in enumerate(group.options):
            for item_index, value in enumerate(option.content_latex):
                if CHOICE_BLANK_PATTERN.search(value):
                    findings.append(
                        AuditFinding(
                            field="options_latex",
                            path=(
                                f"options_latex[{group_index}].options[{option_index}]"
                                f".content_latex[{item_index}]"
                            ),
                            reason="choice_blank_in_options",
                            text=value,
                        )
                    )
    return findings


def _split_math_segments(text: str) -> list[tuple[bool, str]]:
    segments: list[tuple[bool, str]] = []
    index = 0
    while index < len(text):
        start = _find_unescaped(text, "$", index)
        if start < 0:
            if index < len(text):
                segments.append((False, text[index:]))
            break
        if start > index:
            segments.append((False, text[index:start]))

        delimiter = "$$" if text.startswith("$$", start) else "$"
        content_start = start + len(delimiter)
        end = _find_unescaped(text, delimiter, content_start)
        if end < 0:
            segments.append((False, text[start:]))
            break
        segments.append((True, text[content_start:end]))
        index = end + len(delimiter)
    return segments


def _find_unescaped(text: str, token: str, start: int) -> int:
    index = start
    while True:
        index = text.find(token, index)
        if index < 0:
            return -1
        if not _is_escaped(text, index):
            return index
        index += len(token)


def _is_escaped(text: str, index: int) -> bool:
    slash_count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        slash_count += 1
        cursor -= 1
    return slash_count % 2 == 1


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


def build_step35_patch_messages(
    *,
    record: QuestionRecord,
    audit_findings: list[AuditFinding],
    prompt_loader: PromptLoader,
    prompt_ref: str = "step35_latex_patch",
) -> list[dict[str, Any]]:
    system_prompt = prompt_loader.load_system_text(prompt_ref)
    user_prompt = prompt_loader.render_user_text(prompt_ref).replace(
        "{record_and_audit_findings_json}",
        _record_and_patch_findings_json(record, audit_findings),
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


def call_step35_patch_review(
    *,
    record: QuestionRecord,
    call_spec_path: Path,
    audit_findings: list[AuditFinding] | None = None,
    client: LLMClient | None = None,
    prompt_loader: PromptLoader | None = None,
    env: dict[str, str] | None = None,
) -> Step35PatchReview:
    prompt_loader = prompt_loader or PromptLoader()
    findings = audit_findings if audit_findings is not None else audit_record(record)
    resolved = load_and_resolve_call_spec(call_spec_path, prompt_loader=prompt_loader)
    if resolved.call_spec.mode != "patch_normalize":
        raise ValidationError(f"Step3.5 patch call spec mode must be patch_normalize, got {resolved.call_spec.mode}")
    messages = build_step35_patch_messages(
        record=record,
        audit_findings=findings,
        prompt_loader=prompt_loader,
        prompt_ref=resolved.call_spec.prompt,
    )
    tool_schema = resolve_tool_schema(resolved.call_spec.tool_schema or "Step35LatexPatch")
    payload = build_request_payload(
        resolved,
        messages=messages,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": resolved.call_spec.tool_name,
                    "description": "提交 Step3.5 局部 LaTeX 规范化补丁。",
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
    return parse_step35_patch_response(
        response,
        expected_tool_name=resolved.call_spec.tool_name,
        expected_question_no=record.question_no,
    )


def call_step35_patch_record(
    *,
    record: QuestionRecord,
    call_spec_path: Path,
    audit_findings: list[AuditFinding] | None = None,
    client: LLMClient | None = None,
    prompt_loader: PromptLoader | None = None,
    env: dict[str, str] | None = None,
) -> tuple[QuestionRecord, Step35PatchReview]:
    review = call_step35_patch_review(
        record=record,
        call_spec_path=call_spec_path,
        audit_findings=audit_findings,
        client=client,
        prompt_loader=prompt_loader,
        env=env,
    )
    return apply_step35_patch(record, review), review


def parse_step35_response(
    response: LLMResponse,
    *,
    expected_tool_name: str,
    expected_question_no: int,
) -> QuestionRecord:
    payload = parse_tool_call_arguments(response, expected_tool_name=expected_tool_name)
    ensure_options_placeholders_in_payload(payload)
    record = QuestionRecord.from_dict(payload)
    if record.question_no != expected_question_no:
        raise ValidationError(
            f"Step3.5 returned question_no={record.question_no}, expected {expected_question_no}"
        )
    return record


def parse_step35_patch_response(
    response: LLMResponse,
    *,
    expected_tool_name: str,
    expected_question_no: int,
) -> Step35PatchReview:
    payload = parse_tool_call_arguments(response, expected_tool_name=expected_tool_name)
    review = Step35PatchReview.from_dict(payload)
    if review.question_no != expected_question_no:
        raise ValidationError(
            f"Step3.5 patch returned question_no={review.question_no}, expected {expected_question_no}"
        )
    return review


def apply_step35_patch(record: QuestionRecord, review: Step35PatchReview) -> QuestionRecord:
    if review.question_no != record.question_no:
        raise ValidationError(
            f"Step3.5 patch question_no={review.question_no}, expected {record.question_no}"
        )
    payload = copy.deepcopy(record.to_dict())
    before_assets = _record_asset_placeholders(payload)
    for edit in review.edits:
        _apply_step35_patch_edit(payload, edit)
    after_assets = _record_asset_placeholders(payload)
    if before_assets != after_assets:
        raise ValidationError(
            f"Step3.5 patch changed asset placeholders for q{record.question_no}: "
            f"before={before_assets}, after={after_assets}"
        )
    return QuestionRecord.from_dict(payload)


def _apply_step35_patch_edit(payload: dict[str, Any], edit: Step35PatchEdit) -> None:
    text_match = PATCH_TEXT_PATH_PATTERN.fullmatch(edit.path)
    if text_match:
        field_name = text_match.group(1)
        index = int(text_match.group(2))
        values = payload.get(field_name)
        if not isinstance(values, list) or index >= len(values):
            raise ValidationError(f"Step3.5 patch path out of range: {edit.path}")
        current = values[index]
        if current != edit.expected_old:
            raise ValidationError(
                f"Step3.5 patch expected_old mismatch at {edit.path}: "
                f"expected={edit.expected_old!r}, actual={current!r}"
            )
        values[index] = edit.new
        return

    option_match = PATCH_OPTION_PATH_PATTERN.fullmatch(edit.path)
    if option_match:
        group_index = int(option_match.group(1))
        option_index = int(option_match.group(2))
        item_index = int(option_match.group(3))
        groups = payload.get("options_latex")
        if not isinstance(groups, list) or group_index >= len(groups):
            raise ValidationError(f"Step3.5 patch path out of range: {edit.path}")
        group = groups[group_index]
        if not isinstance(group, dict):
            raise ValidationError(f"Step3.5 patch path does not point to an option group: {edit.path}")
        options = group.get("options")
        if not isinstance(options, list) or option_index >= len(options):
            raise ValidationError(f"Step3.5 patch path out of range: {edit.path}")
        option = options[option_index]
        if not isinstance(option, dict):
            raise ValidationError(f"Step3.5 patch path does not point to an option: {edit.path}")
        values = option.get("content_latex")
        if not isinstance(values, list) or item_index >= len(values):
            raise ValidationError(f"Step3.5 patch path out of range: {edit.path}")
        current = values[item_index]
        if current != edit.expected_old:
            raise ValidationError(
                f"Step3.5 patch expected_old mismatch at {edit.path}: "
                f"expected={edit.expected_old!r}, actual={current!r}"
            )
        values[item_index] = edit.new
        return

    raise ValidationError(f"Step3.5 patch path is not allowed: {edit.path}")


def _record_asset_placeholders(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for field_name in ("stem_latex", "answer_latex", "analysis_latex"):
        for segment in payload.get(field_name, []):
            values.extend(ASSET_PLACEHOLDER_PATTERN.findall(str(segment)))
    for group in payload.get("options_latex", []):
        if not isinstance(group, dict):
            continue
        for option in group.get("options", []):
            if not isinstance(option, dict):
                continue
            for segment in option.get("content_latex", []):
                values.extend(ASSET_PLACEHOLDER_PATTERN.findall(str(segment)))
    return sorted(values)


def write_step35_outputs(
    *,
    run_context: RunContext,
    normalized_records: list[QuestionRecord],
    before_findings: dict[int, list[AuditFinding]] | None = None,
    after_findings: dict[int, list[AuditFinding]] | None = None,
    write_back: bool = False,
    elapsed_seconds: float | None = None,
    retry_count: int = 0,
    attempt_count: int | None = None,
    worker_count: int = 1,
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
        "worker_count": worker_count,
        "retry_count": retry_count,
        "attempt_count": attempt_count if attempt_count is not None else len(normalized_payloads),
    }
    if elapsed_seconds is not None:
        summary["elapsed_seconds"] = elapsed_seconds
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


def _record_and_patch_findings_json(record: QuestionRecord, findings: list[AuditFinding]) -> str:
    import json

    payload = {
        "record": record.to_dict(),
        "audit_findings": [item.to_dict() for item in findings],
        "patch_targets": _patch_targets(record),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _patch_targets(record: QuestionRecord) -> list[dict[str, str]]:
    import json

    targets: list[dict[str, str]] = []
    for index, value in enumerate(record.stem_latex):
        targets.append(_patch_target(f"stem_latex[{index}]", value))
    for index, value in enumerate(record.answer_latex):
        targets.append(_patch_target(f"answer_latex[{index}]", value))
    for index, value in enumerate(record.analysis_latex):
        targets.append(_patch_target(f"analysis_latex[{index}]", value))
    for group_index, group in enumerate(record.options_latex):
        for option_index, option in enumerate(group.options):
            for item_index, value in enumerate(option.content_latex):
                targets.append(
                    _patch_target(
                        f"options_latex[{group_index}].options[{option_index}].content_latex[{item_index}]",
                        value,
                    )
                )
    return targets


def _patch_target(path: str, value: str) -> dict[str, str]:
    import json

    return {
        "path": path,
        "expected_old_json": json.dumps(value, ensure_ascii=True),
    }
