from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
from exam_import.schemas.answer_table import AnswerTableReview
from exam_import.schemas.common import ValidationError
from exam_import.schemas.question_record import QuestionRecord
from exam_import.schemas.visual_asset import VisualAssetReview


@dataclass(frozen=True)
class Step4SyncSummary:
    changed_question_count: int
    added_placeholder_count: int
    moved_placeholder_count: int
    removed_placeholder_count: int
    merged_answer_entry_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_question_count": self.changed_question_count,
            "added_placeholder_count": self.added_placeholder_count,
            "moved_placeholder_count": self.moved_placeholder_count,
            "removed_placeholder_count": self.removed_placeholder_count,
            "merged_answer_entry_count": self.merged_answer_entry_count,
        }


def call_visual_asset_review(
    *,
    compact_payload: dict[str, Any],
    annotated_page_image: Path,
    call_spec_path: Path,
    client: LLMClient | None = None,
    prompt_loader: PromptLoader | None = None,
    env: dict[str, str] | None = None,
) -> VisualAssetReview:
    prompt_loader = prompt_loader or PromptLoader()
    if not annotated_page_image.exists():
        raise FileNotFoundError(f"Missing Step4 annotated page image: {annotated_page_image}")
    resolved = load_and_resolve_call_spec(call_spec_path, prompt_loader=prompt_loader)
    system_prompt = prompt_loader.load_system_text(resolved.call_spec.prompt)
    user_prompt = prompt_loader.render_user_text(resolved.call_spec.prompt).replace(
        "{compact_json}",
        _json_dump(compact_payload),
    )
    user_prompt = user_prompt.replace("{output_rule}", _output_rule(resolved.call_spec.tool_name, kind="visual_assets"))
    user_prompt = user_prompt.replace("{final_rule}", f"必须调用工具 {resolved.call_spec.tool_name}，不要在正文中输出任何内容。")
    page = int(compact_payload.get("page") or 0)
    payload = build_request_payload(
        resolved,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"第 {page} 页标注图："},
                    {"type": "image_url", "image_url": {"url": data_uri(annotated_page_image)}},
                    {"type": "text", "text": user_prompt},
                ],
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": resolved.call_spec.tool_name,
                    "description": "提交 Step4 单页视觉资产占位对账结果。",
                    "parameters": resolve_tool_schema(resolved.call_spec.tool_schema or "submit_step4_visual_assets"),
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
    parsed = parse_tool_call_arguments(response, expected_tool_name=resolved.call_spec.tool_name)
    return VisualAssetReview.from_dict(parsed)


def call_answer_table_review(
    *,
    compact_payload: dict[str, Any],
    annotated_page_image: Path,
    call_spec_path: Path,
    client: LLMClient | None = None,
    prompt_loader: PromptLoader | None = None,
    env: dict[str, str] | None = None,
) -> AnswerTableReview:
    prompt_loader = prompt_loader or PromptLoader()
    if not annotated_page_image.exists():
        raise FileNotFoundError(f"Missing Step4 annotated page image: {annotated_page_image}")
    resolved = load_and_resolve_call_spec(call_spec_path, prompt_loader=prompt_loader)
    system_prompt = prompt_loader.load_system_text(resolved.call_spec.prompt)
    user_prompt = prompt_loader.render_user_text(resolved.call_spec.prompt).replace(
        "{compact_json}",
        _json_dump(compact_payload),
    )
    user_prompt = user_prompt.replace("{output_rule}", _output_rule(resolved.call_spec.tool_name, kind="answer_tables"))
    user_prompt = user_prompt.replace("{final_rule}", f"必须调用工具 {resolved.call_spec.tool_name}，不要在正文中输出任何内容。")
    page = int(compact_payload.get("page") or 0)
    payload = build_request_payload(
        resolved,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"第 {page} 页标注图："},
                    {"type": "image_url", "image_url": {"url": data_uri(annotated_page_image)}},
                    {"type": "text", "text": user_prompt},
                ],
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": resolved.call_spec.tool_name,
                    "description": "提交 Step4 答案页表格分类和最终答案抽取结果。",
                    "parameters": resolve_tool_schema(resolved.call_spec.tool_schema or "submit_step4_answer_tables"),
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
    parsed = parse_tool_call_arguments(response, expected_tool_name=resolved.call_spec.tool_name)
    return AnswerTableReview.from_dict(parsed)


def apply_step4_sync(
    *,
    records: list[QuestionRecord],
    visual_review: VisualAssetReview,
    answer_table_review: AnswerTableReview | None = None,
) -> tuple[list[QuestionRecord], Step4SyncSummary]:
    payloads = {record.question_no: record.to_dict() for record in records}
    changed_questions: set[int] = set()
    added = 0
    moved = 0
    removed = 0
    merged_answers = 0

    for asset in visual_review.assets:
        question_no = asset.question_no
        if question_no not in payloads or question_no <= 0:
            continue
        record = payloads[question_no]
        if asset.action == "keep_existing" or asset.action == "ignore_asset" or asset.action == "review_required":
            continue
        if asset.action == "remove_placeholder":
            removed += _remove_placeholder(record, asset.placeholder, asset.source_field, asset.option_group_no, asset.option_label)
            changed_questions.add(question_no)
        elif asset.action == "add_placeholder":
            if _append_placeholder(record, asset):
                added += 1
                changed_questions.add(question_no)
        elif asset.action == "move_placeholder":
            removed += _remove_placeholder(record, asset.placeholder, asset.source_field, asset.option_group_no, asset.option_label)
            if _append_placeholder(record, asset):
                moved += 1
                changed_questions.add(question_no)

    if answer_table_review:
        for table in answer_table_review.tables:
            if table.role != "answer_key_table":
                continue
            for entry in table.entries:
                record = payloads.get(entry.question_no)
                if not record:
                    continue
                for item in entry.answer_markdown:
                    answer_markdown = record["answer_markdown"]
                    if item not in answer_markdown:
                        answer_markdown.append(item)
                        merged_answers += 1
                        changed_questions.add(entry.question_no)

    merged_records = [
        QuestionRecord.from_dict(payloads[qno]) for qno in sorted(payloads)
    ]
    return merged_records, Step4SyncSummary(
        changed_question_count=len(changed_questions),
        added_placeholder_count=added,
        moved_placeholder_count=moved,
        removed_placeholder_count=removed,
        merged_answer_entry_count=merged_answers,
    )


def write_step4_outputs(
    *,
    run_context: RunContext,
    visual_review: VisualAssetReview,
    answer_table_review: AnswerTableReview | None,
    merged_records: list[QuestionRecord],
    sync_summary: Step4SyncSummary,
) -> dict[str, Any]:
    write_json(run_context.step2_run_dir / "visual_asset_assignment.json", visual_review.to_dict())
    if answer_table_review is not None:
        write_json(run_context.step2_run_dir / "answer_table_extraction.json", answer_table_review.to_dict())
    write_json(
        run_context.step2_run_dir / "step4_question_bank_sync.json",
        {
            "run_id": run_context.run_id,
            "summary": sync_summary.to_dict(),
            "question_numbers": [record.question_no for record in merged_records],
        },
    )
    write_question_bank(run_context.question_bank_dir, merged_records)
    return sync_summary.to_dict()


def _append_placeholder(record: dict[str, Any], asset: Any) -> bool:
    placeholder = asset.placeholder
    if not placeholder:
        return False
    target_field = asset.target_field
    if target_field == "options_markdown":
        option_segments = _locate_option_segments(record, asset.option_group_no, asset.option_label)
        if placeholder not in option_segments:
            option_segments.append(placeholder)
            return True
        return False
    segments = record[target_field]
    if placeholder not in segments:
        segments.append(placeholder)
        return True
    return False


def _remove_placeholder(
    record: dict[str, Any],
    placeholder: str,
    source_field: str,
    option_group_no: str,
    option_label: str,
) -> int:
    if not placeholder:
        return 0
    removed = 0
    if source_field == "options_markdown":
        segments = _locate_option_segments(record, option_group_no, option_label)
        while placeholder in segments:
            segments.remove(placeholder)
            removed += 1
        return removed
    if source_field == "none":
        for field_name in ("stem_markdown", "answer_markdown", "analysis_markdown"):
            segments = record[field_name]
            while placeholder in segments:
                segments.remove(placeholder)
                removed += 1
        for group in record["options_markdown"]:
            for option in group["options"]:
                segments = option["content_markdown"]
                while placeholder in segments:
                    segments.remove(placeholder)
                    removed += 1
        return removed
    segments = record[source_field]
    while placeholder in segments:
        segments.remove(placeholder)
        removed += 1
    return removed


def _locate_option_segments(record: dict[str, Any], option_group_no: str, option_label: str) -> list[str]:
    for group in record["options_markdown"]:
        if group["no"] != option_group_no:
            continue
        for option in group["options"]:
            if option["label"] == option_label:
                return option["content_markdown"]
    raise ValidationError(
        f"Option target not found for option_group_no={option_group_no!r}, option_label={option_label!r}"
    )


def _json_dump(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _output_rule(tool_name: str, *, kind: str) -> str:
    if kind == "visual_assets":
        return (
            f"输出时必须调用工具 {tool_name}。\n"
            "- tool 参数根字段必须包含 assets、risks。\n"
            "- assets 必须覆盖输入中的每个 assets[] 项，各出现一次。\n"
            "- risks 没有内容时必须返回空数组。"
        )
    if kind == "answer_tables":
        return (
            f"输出时必须调用工具 {tool_name}。\n"
            "- tool 参数根字段必须包含 tables、risks。\n"
            "- tables 必须覆盖输入中的每个 tables[] 项，各出现一次。\n"
            "- risks 没有内容时必须返回空数组。"
        )
    raise ValueError(f"Unsupported Step4 output rule kind: {kind}")
