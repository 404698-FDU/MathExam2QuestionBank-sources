from __future__ import annotations

import argparse
import concurrent.futures
import traceback
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError



CODE_ROOT = Path(__file__).resolve().parents[1]
WORKTREE_ROOT = Path(__file__).resolve().parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from common_blocks import unique_str  # noqa: E402
from common_io import data_uri, read_json, write_json, write_jsonl  # noqa: E402
from common_llm import load_token, post_chat_completion  # noqa: E402
from common_math_text import split_math_segments  # noqa: E402
from common_step2_crops import crop_labels_to_images, load_step2_question_surface_labels  # noqa: E402
from common_token_budget import DEFAULT_OUTPUT_RESERVE_TOKENS, configure_token_budget_env  # noqa: E402
from step3_schema import JSON_SCHEMA, Step3Record  # noqa: E402

LATEX_COMMANDS = (
    "frac",
    "sqrt",
    "Rightarrow",
    "Leftarrow",
    "Leftrightarrow",
    "times",
    "cdot",
    "leq",
    "geq",
    "neq",
    "in",
    "notin",
    "infty",
    "left",
    "right",
    "begin",
    "end",
    "overrightarrow",
    "vec",
    "mathbf",
    "mathbb",
    "mathrm",
    "sin",
    "cos",
    "tan",
    "log",
    "ln",
    "Delta",
    "varphi",
    "theta",
    "alpha",
    "beta",
    "gamma",
    "pi",
    "cup",
    "cap",
)
DOUBLE_ESCAPED_LATEX_RE = re.compile(
    r"\\\\(?=(?:" + "|".join(map(re.escape, LATEX_COMMANDS)) + r")\b|[{}])"
)
META_RE = re.compile(r"(?:作为|我会|我将|无法|不能|根据输入|根据OCR|OCR内容|字段|schema|JSON)")
MATH_COMMAND_RE = re.compile(
    r"\\(?:frac|sqrt|vec|overrightarrow|cdot|times|leq|geq|neq|in|notin|mathbb|mathbf|mathrm|text|sin|cos|tan|log|ln|alpha|beta|gamma|pi|theta|Delta|left|right)\b"
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
VISUAL_PLACEHOLDER_BLOCK_RE = re.compile(r"^<(?:image|chart|figure)\d+>$", re.IGNORECASE)
ALLOWED_MATH_CONNECTORS = {"且", "或", "并", "及", "和", "与"}


def load_packet_blocks(packet_dir: Path) -> dict[str, dict[str, Any]]:
    blocks: dict[str, dict[str, Any]] = {}
    for path in sorted(packet_dir.glob("page_*.json")):
        packet = read_json(path, {})
        page = packet.get("page")
        for block in packet.get("blocks") or []:
            label = str(block.get("label") or "")
            if not label:
                continue
            row = dict(block)
            row.setdefault("page", page)
            blocks[label] = row
    return blocks


def load_blocks_with_full_text(run_dir: Path, packet_name: str, layout_name: str) -> dict[str, dict[str, Any]]:
    blocks = load_packet_blocks(run_dir / packet_name)
    layout_items = read_json(run_dir / layout_name, [])
    full_by_block_id = {str(item.get("block_id") or ""): item for item in layout_items if item.get("block_id")}
    for block in blocks.values():
        full = full_by_block_id.get(str(block.get("block_id") or ""))
        if full and full.get("text") is not None:
            block["text"] = str(full.get("text") or "")
    return blocks


def placeholder_kind(block: dict[str, Any]) -> str | None:
    label = str(block.get("label") or "")
    local = label.split("-")[-1]
    block_type = str(block.get("type") or "").lower()
    if "footnote" in block_type or "caption" in block_type:
        return None
    if "table" in block_type:
        return "table"
    if "chart" in block_type:
        return "chart"
    if "image_caption" in block_type:
        return "image_caption"
    if "image" in block_type:
        return "image"
    if block.get("figure_id") or local.startswith("P"):
        return "figure"
    return None


def placeholder_for(kind: str, counters: dict[str, int]) -> str:
    counters[kind] = counters.get(kind, 0) + 1
    return f"<{kind}{counters[kind]:02d}>"


def last_unescaped_dollar_index(text: str) -> int:
    escaped = False
    last = -1
    for index, char in enumerate(text):
        if char == "\\" and not escaped:
            escaped = True
            continue
        if char == "$" and not escaped:
            last = index
        escaped = False
    return last


def clean_ocr_text(
    text: str,
    label: str,
    block: dict[str, Any],
    counters: dict[str, int],
    placeholder_map: list[dict[str, Any]],
) -> str:
    if unescaped_dollar_count(text) % 2 == 0:
        return text
    index = last_unescaped_dollar_index(text)
    if index < 0:
        return text
    placeholder = placeholder_for("ocr_gap", counters)
    placeholder_map.append(
        {
            "label": label,
            "kind": "ocr_gap",
            "placeholder": placeholder,
            "page": block.get("page"),
            "bbox": block.get("bbox"),
            "reason": "unbalanced_math_in_ocr_block",
            "omitted_text": text[index:],
        }
    )
    return (text[:index].rstrip() + " " + placeholder).strip()


def sanitize_blocks(
    labels: list[str],
    blocks_by_label: dict[str, dict[str, Any]],
    counters: dict[str, int],
    placeholder_map: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    existing_placeholders = {str(item["label"]): str(item["placeholder"]) for item in placeholder_map if item.get("label")}
    for label in labels:
        block = blocks_by_label.get(str(label))
        if not block:
            sanitized.append({"label": str(label), "missing": True, "text": ""})
            continue
        kind = placeholder_kind(block)
        text = str(block.get("text") or "")
        row = {
            "label": str(label),
            "type": block.get("type"),
            "page": block.get("page"),
            "bbox": block.get("bbox"),
        }
        if kind:
            placeholder = existing_placeholders.get(str(label))
            if not placeholder:
                placeholder = placeholder_for(kind, counters)
                placeholder_map.append(
                    {
                        "label": str(label),
                        "kind": kind,
                        "placeholder": placeholder,
                        "page": block.get("page"),
                        "bbox": block.get("bbox"),
                        "figure_id": block.get("figure_id"),
                        "html": text if kind == "table" and text.lstrip().lower().startswith("<table") else "",
                    }
                )
                existing_placeholders[str(label)] = placeholder
            row["text"] = placeholder
            row["placeholder"] = placeholder
            row["omitted_text"] = True
        else:
            row["text"] = clean_ocr_text(text, str(label), block, counters, placeholder_map)
        sanitized.append(row)
    return sanitized


def normalize_issues(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    issues: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            issue_type = str(item.get("type") or item.get("code") or "llm_issue")
            message = str(item.get("message") or item.get("evidence") or item.get("reason") or "")
            severity = str(item.get("severity") or "warning")
        else:
            issue_type = "llm_issue"
            message = str(item)
            severity = "warning"
        issues.append({"type": issue_type, "message": message, "severity": severity})
    return issues


def append_issue(record: dict[str, Any], issue_type: str, message: str, severity: str = "warning") -> None:
    record.setdefault("issues", [])
    record["issues"].append({"type": issue_type, "message": message, "severity": severity})


def unescaped_dollar_count(text: str) -> int:
    count = 0
    escaped = False
    for char in text:
        if char == "\\" and not escaped:
            escaped = True
            continue
        if char == "$" and not escaped:
            count += 1
        escaped = False
    return count


LATEX_CONTROL_ESCAPES = {
    "\x07": r"\a",
    "\x08": r"\b",
    "\t": r"\t",
    "\x0b": r"\v",
    "\x0c": r"\f",
    "\x1b": r"\e",
}


def restore_latex_control_escapes(text: str) -> str:
    for char, replacement in LATEX_CONTROL_ESCAPES.items():
        text = text.replace(char, replacement)
    return text


def normalize_double_escaped_latex(text: str) -> str:
    return DOUBLE_ESCAPED_LATEX_RE.sub(r"\\", text)


def normalize_latex_blocks(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    blocks: list[str] = []
    for raw_item in raw_items:
        if raw_item is None:
            continue
        text = restore_latex_control_escapes(str(raw_item)).replace("\r\n", "\n").replace("\r", "\n").strip()
        text = normalize_double_escaped_latex(text)
        if not text:
            continue
        for part in text.split("\n"):
            cleaned = part.strip()
            if cleaned:
                blocks.append(cleaned)
    return blocks


def strip_option_prefix(label: str, text: str) -> str:
    label = re.escape(label.strip().upper())
    patterns = [
        rf"^\s*[（(]\s*{label}\s*[）)]\s*",
        rf"^\s*{label}\s*[.．、:：]\s*",
    ]
    stripped = text
    for pattern in patterns:
        stripped = re.sub(pattern, "", stripped, count=1, flags=re.IGNORECASE)
    return stripped.strip()


def normalize_options_latex(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    options: dict[str, list[str]] = {}
    for key, raw_blocks in value.items():
        label = str(key).strip().upper()
        if not label:
            continue
        blocks = [
            stripped
            for block in normalize_latex_blocks(raw_blocks)
            if (stripped := strip_option_prefix(label, block))
        ]
        if blocks:
            options[label] = blocks
    return options


def latex_blocks_text(value: Any) -> str:
    return "\n".join(normalize_latex_blocks(value))


def latex_blocks_empty(value: Any) -> bool:
    return not normalize_latex_blocks(value)


def remove_placeholders_from_blocks(blocks: list[str], placeholders: set[str]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        text = str(block)
        for placeholder in placeholders:
            text = text.replace(placeholder, "")
        text = re.sub(r"\s{2,}", " ", text).strip()
        if text:
            out.append(text)
    return out


def table_records_from_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for item in source.get("non_text_placeholders") or []:
        if not isinstance(item, dict) or str(item.get("kind")) != "table":
            continue
        placeholder = str(item.get("placeholder") or "").strip()
        html_text = re.sub(r"\s+", " ", str(item.get("html") or "").strip())
        label = str(item.get("label") or "").strip()
        if not placeholder or not html_text.lower().startswith("<table"):
            continue
        tables.append(
            {
                "label": label,
                "placeholder": placeholder,
                "html": html_text,
                "page": item.get("page"),
                "bbox": item.get("bbox"),
                "source": "question" if label.startswith("Q-") else "answer" if label.startswith("A-") else "unknown",
            }
        )
    return tables


def apply_visual_asset_policy(record: dict[str, Any], source: dict[str, Any]) -> None:
    visual_placeholders = {
        str(item.get("placeholder"))
        for item in source.get("non_text_placeholders") or []
        if str(item.get("kind")) in {"image", "chart", "figure", "image_caption"} and item.get("placeholder")
    }
    if not visual_placeholders:
        return
    for field in ("stem_latex", "answer_latex", "analysis_latex", "rubric_latex"):
        raw_value = record.get(field)
        blocks = [str(item) for item in raw_value] if isinstance(raw_value, list) else normalize_latex_blocks(raw_value)
        record[field] = remove_placeholders_from_blocks(blocks, visual_placeholders)
    options = record.get("options_latex") if isinstance(record.get("options_latex"), dict) else {}
    cleaned_options: dict[str, list[str]] = {}
    for key, value in options.items():
        blocks = [str(item) for item in value] if isinstance(value, list) else normalize_latex_blocks(value)
        cleaned = remove_placeholders_from_blocks(blocks, visual_placeholders)
        if cleaned:
            cleaned_options[str(key)] = cleaned
    record["options_latex"] = cleaned_options


def cleanup_unknown_table_placeholders(record: dict[str, Any], source: dict[str, Any]) -> None:
    all_known = {
        str(item.get("placeholder"))
        for item in source.get("non_text_placeholders") or []
        if str(item.get("kind")) == "table" and item.get("placeholder")
    }
    question_known = {
        str(item.get("placeholder"))
        for item in source.get("non_text_placeholders") or []
        if str(item.get("kind")) == "table" and str(item.get("label") or "").startswith("Q-") and item.get("placeholder")
    }
    pattern = re.compile(r"<table\d+>", re.IGNORECASE)

    def clean_block(text: str, allowed: set[str]) -> str:
        def replace(match: re.Match[str]) -> str:
            placeholder = match.group(0)
            if placeholder in allowed:
                return placeholder
            if placeholder in all_known:
                append_issue(record, "disallowed_table_placeholder_removed", f"{placeholder} belongs to another source section and was removed from this field.", "warning")
            else:
                append_issue(record, "unknown_table_placeholder_removed", f"{placeholder} was not present in current table metadata and was removed.", "warning")
            return ""

        return re.sub(r"\s{2,}", " ", pattern.sub(replace, text)).strip()

    for field in ("stem_latex",):
        raw_value = record.get(field)
        blocks = [str(item) for item in raw_value] if isinstance(raw_value, list) else normalize_latex_blocks(raw_value)
        record[field] = [cleaned for block in blocks for cleaned in [clean_block(block, question_known)] if cleaned]
    for field in ("answer_latex", "analysis_latex", "rubric_latex"):
        raw_value = record.get(field)
        blocks = [str(item) for item in raw_value] if isinstance(raw_value, list) else normalize_latex_blocks(raw_value)
        record[field] = [cleaned for block in blocks for cleaned in [clean_block(block, all_known)] if cleaned]
    options = record.get("options_latex") if isinstance(record.get("options_latex"), dict) else {}
    cleaned_options: dict[str, list[str]] = {}
    for key, value in options.items():
        blocks = [str(item) for item in value] if isinstance(value, list) else normalize_latex_blocks(value)
        cleaned = [cleaned_block for block in blocks for cleaned_block in [clean_block(block, question_known)] if cleaned_block]
        if cleaned:
            cleaned_options[str(key)] = cleaned
    record["options_latex"] = cleaned_options


BLANK_UNDERLINE_RE = re.compile(
    r"\$?\s*\\underline\s*\{\s*(?:\\(?:quad|qquad)|\\hspace\s*\{[^{}]*\}|\s)+\s*\}\s*\$?"
)
BLANK_TOKEN_RE = re.compile(r"<(?:blank|choice_blank)>")
TEXT_BLANK_UNDERSCORE_RE = re.compile(r"(?<!\\)_{2,}")
EMPTY_CHOICE_PAREN_RE = re.compile(r"(?:（\s*）|\(\s*\))")
WRAPPED_CHOICE_BLANK_RE = re.compile(r"(?:（\s*<choice_blank>\s*）|\(\s*<choice_blank>\s*\))")


def math_segment_parts(segment: str) -> tuple[str, str, str]:
    if segment.startswith("$$") and segment.endswith("$$") and len(segment) >= 4:
        return "$$", segment[2:-2], "$$"
    if segment.startswith(r"\(") and segment.endswith(r"\)"):
        return r"\(", segment[2:-2], r"\)"
    if segment.startswith(r"\[") and segment.endswith(r"\]"):
        return r"\[", segment[2:-2], r"\]"
    if segment.startswith("$") and segment.endswith("$") and len(segment) >= 2:
        return "$", segment[1:-1], "$"
    return "", segment, ""


def canonical_blank_token(text: str, token: str) -> str:
    text = text.replace("< blank >", "<blank>").replace("< choice_blank >", "<choice_blank>")
    if token == "<blank>":
        text = text.replace("<choice_blank>", "<blank>")
    elif token == "<choice_blank>":
        text = text.replace("<blank>", "<choice_blank>")
    return text


def normalize_blank_marks(record: dict[str, Any]) -> None:
    question_type = str(record.get("question_type") or "")
    if question_type not in {"fill_blank", "single_choice", "multiple_choice"}:
        return
    token = "<choice_blank>" if question_type in {"single_choice", "multiple_choice"} else "<blank>"

    def normalize_block(text: str) -> str:
        text = canonical_blank_token(text, token)
        text = BLANK_UNDERLINE_RE.sub(token, text)
        text = re.sub(r"(?<!\\)\\_(?![A-Za-z])", token, text)
        segments: list[str] = []
        repaired = False
        for is_math, segment in split_math_segments(text):
            if is_math:
                found_tokens = BLANK_TOKEN_RE.findall(segment)
                if not found_tokens:
                    segments.append(segment)
                    continue
                opener, inner, closer = math_segment_parts(segment)
                inner = BLANK_TOKEN_RE.sub("", inner)
                inner = re.sub(r"\s{2,}", " ", inner).strip()
                if inner:
                    segments.append(f"{opener}{inner}{closer}")
                segments.append(" " + " ".join(found_tokens))
                repaired = True
                continue
            segment = TEXT_BLANK_UNDERSCORE_RE.sub(token, segment)
            if question_type in {"single_choice", "multiple_choice"}:
                segment = WRAPPED_CHOICE_BLANK_RE.sub(token, segment)
                segment = EMPTY_CHOICE_PAREN_RE.sub(token, segment)
            elif question_type == "fill_blank":
                segment = re.sub(r"（\s{1,}）|\(\s{1,}\)", token, segment)
            segments.append(segment)
        normalized = re.sub(r"\s{2,}", " ", "".join(segments)).strip()
        if repaired:
            append_issue(record, "blank_token_moved_out_of_math", f"{token} appeared inside a math segment and was moved outside.", "info")
        return normalized

    blocks = normalize_latex_blocks(record.get("stem_latex"))
    record["stem_latex"] = [normalize_block(block) for block in blocks]


def blank_tokens_inside_math(text: str) -> list[str]:
    tokens: list[str] = []
    for is_math, segment in split_math_segments(text):
        if is_math:
            tokens.extend(BLANK_TOKEN_RE.findall(segment))
    return tokens


def strip_top_question_no(blocks: list[str], qno: int) -> list[str]:
    if not blocks:
        return blocks
    first_block = str(blocks[0])
    marker = re.search(rf"(?<!\d){qno}\s*[.．、]\s*", first_block)
    if marker:
        first = first_block[marker.end() :].strip()
        if first:
            return [first] + blocks[1:]
        return blocks[1:]
    first = re.sub(rf"^\s*{qno}\s*[.．、]\s*", "", first_block).strip()
    if first:
        return [first] + blocks[1:]
    return blocks[1:]


def validate_record(record: dict[str, Any], source: dict[str, Any]) -> None:
    qno = int(source["question_no"])
    if int(record.get("question_no") or 0) != qno:
        append_issue(record, "question_no_mismatch", f"Model returned question_no={record.get('question_no')}; expected {qno}.", "error")
        record["question_no"] = qno
    stem = latex_blocks_text(record.get("stem_latex"))
    if re.match(rf"^\s*{qno}\s*[.．、]", stem):
        append_issue(record, "stem_contains_question_no", "stem_latex appears to include the top-level question number.")
    for field in ("stem_latex", "answer_latex", "analysis_latex", "rubric_latex"):
        text = latex_blocks_text(record.get(field))
        if unescaped_dollar_count(text) % 2:
            append_issue(record, "unbalanced_dollar", f"{field} has an odd number of unescaped dollar signs.", "error")
        if re.search(r"!\[[^\]]*]\(|\.(?:png|jpe?g|webp|bmp)\b", text, re.IGNORECASE):
            append_issue(record, "image_path_in_output", f"{field} contains a likely Markdown image path.", "error")
        tokens_in_math = blank_tokens_inside_math(text)
        if tokens_in_math:
            append_issue(record, "blank_token_inside_math", f"{field} contains blank tokens inside math: {', '.join(sorted(set(tokens_in_math)))}.", "error")
    options_obj = record.get("options_latex") if isinstance(record.get("options_latex"), dict) else {}
    for key, value in options_obj.items():
        text = latex_blocks_text(value)
        tokens_in_math = blank_tokens_inside_math(text)
        if tokens_in_math:
            append_issue(record, "blank_token_inside_math", f"options_latex.{key} contains blank tokens inside math: {', '.join(sorted(set(tokens_in_math)))}.", "error")
    placeholders = [str(item.get("placeholder")) for item in source.get("non_text_placeholders") or [] if item.get("placeholder")]
    question_placeholders = [
        str(item.get("placeholder"))
        for item in source.get("non_text_placeholders") or []
        if str(item.get("label") or "").startswith("Q-") and str(item.get("kind")) == "ocr_gap" and item.get("placeholder")
    ]
    options_text = "\n".join(latex_blocks_text(value) for value in options_obj.values())
    rendered_text = "\n".join(latex_blocks_text(record.get(field)) for field in ("stem_latex", "answer_latex", "analysis_latex", "rubric_latex")) + "\n" + options_text
    for placeholder in question_placeholders:
        if placeholder not in rendered_text:
            append_issue(record, "missing_question_placeholder", f"{placeholder} from question blocks is not present in stem/analysis/rubric.")
    if any(str(item.get("kind")) == "ocr_gap" for item in source.get("non_text_placeholders") or []):
        append_issue(record, "ocr_gap_in_input", "Input contained an unclosed OCR math fragment that was replaced by an <ocr_gap..> placeholder.")
    for item in source.get("non_text_placeholders") or []:
        placeholder = str(item.get("placeholder") or "")
        if str(item.get("kind")) == "table" and placeholder and placeholder in rendered_text:
            append_issue(record, "unresolved_table_placeholder", f"{placeholder} remains after table postprocessing.", "error")
    if latex_blocks_empty(record.get("answer_latex")) and source.get("answer_status") == "found":
        append_issue(record, "empty_answer", "Answer blocks were available but answer_latex is empty.", "warning")
    if not placeholders:
        return


def error_record(source: dict[str, Any], model: str, message: str, elapsed: float) -> dict[str, Any]:
    return {
        "question_no": int(source["question_no"]),
        "question_type": "unknown",
        "stem_latex": [],
        "options_latex": {},
        "answer_latex": [],
        "analysis_latex": [],
        "rubric_latex": [],
        "figure_labels": list(source.get("figure_labels") or []),
        "table_labels": list(source.get("table_labels") or []),
        "tables": table_records_from_source(source),
        "visual_assets": list(source.get("visual_assets") or []),
        "option_asset_placeholders": {},
        "source_question_labels": list(source.get("source_question_labels") or []),
        "source_answer_labels": list(source.get("source_answer_labels") or []),
        "issues": [{"type": "step3_standardization_error", "message": message, "severity": "error"}],
        "model": model,
        "standardize_elapsed_seconds": round(elapsed, 3),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def answer_labels_from_items(items: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for item in items:
        labels.extend(str(label) for label in item.get("block_labels") or [] if str(label).strip())
    return unique_str(labels)


def table_and_figure_labels(placeholders: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    table_labels: list[str] = []
    figure_labels: list[str] = []
    for item in placeholders:
        label = str(item.get("label") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not label:
            continue
        if kind == "table":
            table_labels.append(label)
        elif kind in {"image", "chart", "figure", "image_caption"}:
            figure_labels.append(label)
    return unique_str(table_labels), unique_str(figure_labels)


def build_question_payload(
    run_id: str,
    row: dict[str, Any],
    question_blocks: dict[str, dict[str, Any]],
    answer_blocks: dict[str, dict[str, Any]],
    pipeline_mode: str,
) -> dict[str, Any]:
    qno = int(row["question_no"])
    counters: dict[str, int] = {}
    placeholders: list[dict[str, Any]] = []
    question_labels = unique_str(list(row.get("question_labels") or []))
    question_units = sanitize_blocks(question_labels, question_blocks, counters, placeholders)

    answer_items: list[dict[str, Any]] = []
    for item in row.get("answer_items") or []:
        if not isinstance(item, dict):
            continue
        labels = unique_str(list(item.get("block_labels") or []))
        blocks = sanitize_blocks(labels, answer_blocks, counters, placeholders)
        answer_items.append(
            {
                "question_no": qno,
                "role": str(item.get("role") or "answer"),
                "block_labels": labels,
                "blocks": blocks,
                "span_text_excerpt": str(item.get("span_text_excerpt") or ""),
                "continues_previous": bool(item.get("continues_previous") or False),
                "confidence": item.get("confidence"),
                "reason": str(item.get("reason") or ""),
            }
        )

    table_labels, figure_labels = table_and_figure_labels(placeholders)
    return {
        "run_id": run_id,
        "question_no": qno,
        "pipeline_mode": pipeline_mode,
        "question_status": str(row.get("question_status") or ""),
        "answer_status": str(row.get("answer_status") or ""),
        "question_labels": question_labels,
        "question_blocks": question_units,
        "answer_items": answer_items,
        "visual_assets": list(row.get("visual_assets") or []),
        "non_text_placeholders": placeholders,
        "figure_labels": figure_labels,
        "table_labels": table_labels,
        "source_question_labels": question_labels,
        "source_answer_labels": answer_labels_from_items(answer_items),
    }


def load_step2_question_range_labels(run_dir: Path) -> dict[int, list[str]]:
    if not (run_dir / "question_groups.json").exists():
        raise FileNotFoundError(f"Missing question_groups.json for Step3 crop input: {run_dir}")
    labels_by_qno = load_step2_question_surface_labels(run_dir)
    if not labels_by_qno:
        raise ValueError(f"Step2 question_groups.json has no usable question surface labels: {run_dir}")
    return labels_by_qno


def build_payloads_for_run(run_id: str, runs_root: Path) -> list[dict[str, Any]]:
    run_dir = runs_root / f"{run_id}_raw_units"
    alignment = read_json(run_dir / "qa_alignment.json", {})
    if not alignment:
        raise FileNotFoundError(f"Missing qa_alignment.json for {run_id}: {run_dir}")
    summary = read_json(run_dir / "pipeline_summary.json", {})
    pipeline_mode = str(summary.get("mode") or "paper_plus_answer_file")
    question_blocks = load_blocks_with_full_text(run_dir, "question_packets", "question_layout_items.json")
    answer_blocks = load_blocks_with_full_text(run_dir, "answer_packets", "answer_layout_items.json")
    step2_range_labels_by_qno = load_step2_question_range_labels(run_dir)
    payloads: list[dict[str, Any]] = []
    for row in alignment.get("qa_alignment") or []:
        if row.get("question_no") is None:
            continue
        qno = int(row["question_no"])
        if qno not in step2_range_labels_by_qno:
            raise ValueError(f"Missing Step2 question range labels for {run_id} Q{qno}")
        payload = build_question_payload(run_id, row, question_blocks, answer_blocks, pipeline_mode)
        payload["step2_question_range_labels"] = step2_range_labels_by_qno[qno]
        payloads.append(payload)
    return sorted(payloads, key=lambda item: int(item["question_no"]))


def summarize_records(run_id: str, records: list[dict[str, Any]], out_dir: Path) -> dict[str, Any]:
    issue_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for record in records:
        type_counts[str(record.get("question_type") or "unknown")] = type_counts.get(str(record.get("question_type") or "unknown"), 0) + 1
        for issue in record.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            issue_type = str(issue.get("type") or "issue")
            severity = str(issue.get("severity") or "warning")
            issue_counts[issue_type] = issue_counts.get(issue_type, 0) + 1
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
    summary = {
        "run_id": run_id,
        "question_count": len(records),
        "success_count": sum(1 for item in records if not any(issue.get("severity") == "error" for issue in item.get("issues") or [] if isinstance(issue, dict))),
        "error_count": sum(1 for item in records if any(issue.get("severity") == "error" for issue in item.get("issues") or [] if isinstance(issue, dict))),
        "question_type_counts": type_counts,
        "issue_counts": issue_counts,
        "severity_counts": severity_counts,
        "total_elapsed_seconds": round(sum(float(item.get("standardize_elapsed_seconds") or 0.0) for item in records), 3),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    write_json(out_dir / "summary.json", summary)
    write_json(out_dir / "question_bank.json", records)
    write_jsonl(out_dir / "question_bank.jsonl", records)
    return summary

DEFAULT_RUNS_ROOT = WORKTREE_ROOT / "runs_step2_exam_blocks"
DEFAULT_OUTPUT_ROOT = WORKTREE_ROOT / "runs_question_bank"
DEFAULT_MODEL = "Qwen/Qwen3.6-35B-A3B"

SYSTEM_PROMPT = (
    "你是数学试题题库 LaTeX 标准化器。你只做 OCR 转写、边界裁剪、字段归类和格式整理，"
    "不得解题、不得补充条件、不得改题意。输出必须是满足 JSON Schema 的单个 JSON object。"
)

USER_PROMPT = """\
/no_think

输入是一道数学试题的极简 OCR 包。OCR 可能包含上一题末尾或下一题开头，它们只用于边界判断。
请只整理指定 question_no 对应的内容，不要整理上一题或下一题。

输入字段含义：
- question_no：目标题号。
- mode：mixed 或 paper_plus_answer_file。
- boundary_hint：边界提示，只用于判断本题起止。
- ocr_text：mixed 模式下的题目、答案、解析混合 OCR。
- question_ocr：paper_plus_answer_file 模式下的题目 OCR。
- answer_ocr：paper_plus_answer_file 模式下的答案、解析、评分标准 OCR；没有时可省略。

输出要求：
- 必须输出 JSON Schema 中要求的全部字段，且不得增加字段。
- schema_version 必须固定输出为 "step3_json_schema_v1"。
- question_no 必须与输入中的 question_no 一致，类型为整数。
- options_latex 必须始终包含 A、B、C、D 四个键；非选择题也输出四个空数组。
- issues 若没有问题，输出空数组 []。
- issues 中每项必须包含 type、severity、message。
- severity 只能使用 info、warning、error。
- 推荐的 issue type 包括：ocr_unclear、boundary_suspect、answer_missing、type_conflict、foreign_content、content_missing、other。

输出字段含义：
- stem_latex：正式题干、小题、作答空位、必要表格。
- options_latex：A/B/C/D 选项正文。不要包含 A.、B.、(A)、(B) 等选项标签。
- answer_latex：原文中明确独立给出的最终答案、答案表、答案行或各小题答案。
- analysis_latex：原文中的解答步骤、证明过程、计算过程、思路分析、解析、评析、归纳总结。
- rubric_latex：原文中独立成段、独立成表的评分标准、扣分说明、阅卷规则；没有则为空数组。
- issues：只记录 OCR 残缺、边界可疑、答案缺失、题型冲突、内容缺失、混入其他题内容等问题；不要写格式说明或英文元分析。

边界规则：
- 只整理输入中属于 question_no 的内容。
- 必须从指定题号开始，到该题结束为止。
- 若 OCR 中出现上一题尾部或下一题开头，不要将其写入任何输出字段。
- stem_latex 不含题号。
- boundary_hint 只能用于边界判断。
- 若输入中出现明显属于其他题号的内容，必须剔除，并在 issues 中说明。
- 若本题边界无法可靠判断，在保留最可能属于本题内容的同时，在 issues 中记录 boundary_suspect。

题型规则：
- question_type 只能是 fill_blank、single_choice、multiple_choice、solution、unknown。
- 不得输出 choice、选择题、解答题、short_answer 等 schema 外的值。
- 若 A/B/C/D 选项完整出现，按答案数量判定 single_choice 或 multiple_choice。
- fill_blank：无 A/B/C/D 选项，题干有明确空位、横线、待填空白，答案通常是短表达式。
- single_choice：题干有 A/B/C/D 等选项且只有一个正确答案。
- multiple_choice：题干有 A/B/C/D 等选项且多个正确答案。
- solution：没有选项，且题干含 (1)、(2) 等小问或“求、求证、证明、解答、计算”且没有明确空位、横线而要求写出完整解答过程的题。
- unknown：输入残缺到无法可靠判断题型时才使用。

题干和选项规则：
- stem_latex 放正式题干、小题、作答空位、必要表格。
- options_latex 只写选项正文，不含 A/B/C/D 标签。
- 如果选项只有图片或图形，没有文字，可把对应 options_latex 项输出为空数组；图片归属由 Step4 处理。
- 填空位置写为 <blank>，且只能出现在 LaTeX 数学环境外。
- 选择题作答位置写为 <choice_blank>，且只能出现在 LaTeX 数学环境外。
- 不要把答案填回 <blank> 或 <choice_blank>。

答案、解析、评分标准规则：
- answer_latex 只放原文中明确独立给出的最终答案、答案表、答案行或各小题答案。
- 不要从完整解析过程中反推、摘取、概括出 answer_latex。
- 如果答案 OCR 只有完整解析过程，没有独立答案行，则完整放入 analysis_latex，answer_latex 输出空数组，并在 issues 中说明答案未独立给出。
- analysis_latex 放原文中的解答步骤、证明过程、计算过程、思路分析、解析、评析、归纳总结。
- 若解析步骤中夹带“……2分”“……4分”“得分点”等随行评分标记，保留在 analysis_latex 的原位置，不要拆到 rubric_latex。
- rubric_latex 只放原文中独立成段、独立成表的评分标准、扣分说明、阅卷规则。
- 如果没有独立评分标准，rubric_latex 输出空数组。
- 不要把同一段原文重复放入 answer_latex、analysis_latex、rubric_latex 多个字段。
- 不要为了字段完整性而拆分原文；优先保持答案卷原有顺序和表达。

图表规则：
- 原文当前位置是表格时，优先保留为单行 HTML 表格 <table>...</table>；单元格内数学仍按 LaTeX 处理。
- 若 OCR 中已有 <tablexx> 占位符且无法还原表格内容，可以保留该占位符。
- 图片内容不要 OCR 或描述进正文。
- <imagexx>、<chartxx> 如果在题干中，则不写入 stem_latex、answer_latex、analysis_latex、rubric_latex。
- <imagexx>、<chartxx> 只有用于图形选项时，才写入对应 options_latex。
- 图片归属交给 Step4；Step3 不判断图片属于哪道题。

LaTeX 和分段规则：
- 数学内容统一为 LaTeX：行内 $...$，展示 $$...$$。
- answer_latex 中数学答案必须用 $...$。
- 选择题答案字母如 A/B/C/D 写成 $A$。
- stem_latex、answer_latex、analysis_latex、rubric_latex 都是数组。
- 每个数组元素表示渲染时的独立一段，不是 OCR 物理行。
- 每个数组元素必须是单行 JSON 字符串，不得包含真实换行或 \\n。
- 普通文本换行不要使用 \\\\。\\\\ 只允许出现在 aligned、cases、matrix、array 等 LaTeX 环境内部。
- 不要拆分 LaTeX 命令，如 \\neq、\\leq、\\geq、\\in 必须完整保留。
- 所有 LaTeX 反斜杠必须按 JSON 字符串规则正确转义。

保真规则：
- 只做必要的 OCR 常见空格、标点、数学包裹和 LaTeX 格式化。
- 不得摘要、压缩、改写、删减原文与答案、解析、点评。
- 不得新增条件、答案、步骤或图片描述。
- 不得解题，不得根据解析自行推导新答案。

输出规则：
- 只输出 JSON object，不要 Markdown，不要代码块，不要解释。
- 只输出 schema 中的字段。
- options_latex 必须始终包含 A、B、C、D 四个键；非选择题也输出空数组。
- issues 中的每项必须包含 type、severity、message。

输入 JSON：
"""


def call_json_schema(messages: list[dict[str, Any]], model: str, timeout: int, token: str, enable_thinking: bool) -> str:
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "top_p": 0.8,
        "enable_thinking": enable_thinking,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "Step3Record",
                "schema": JSON_SCHEMA,
                "strict": True,
            },
        },
    }
    payload = post_chat_completion(body, timeout=timeout, token=token)
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM API unexpected response: {json.dumps(payload, ensure_ascii=False)[:1200]}") from exc


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def content_warnings(record: Step3Record, compact: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if compact.get("answer_ocr") and not (record.answer_latex or record.analysis_latex or record.rubric_latex):
        warnings.append("answer_ocr was provided but answer/analysis/rubric are all empty")
    for field_name in ("stem_latex", "answer_latex", "analysis_latex", "rubric_latex"):
        for index, item in enumerate(getattr(record, field_name), start=1):
            if META_RE.search(item):
                warnings.append(f"{field_name}[{index}] contains meta analysis")
            if unescaped_dollar_count(item) % 2:
                warnings.append(f"{field_name}[{index}] has unbalanced dollar signs")
            if re.search(r"\\n(?![A-Za-z])", item):
                warnings.append(f"{field_name}[{index}] contains literal newline escape")
    for label, values in record.options_latex.model_dump().items():
        for index, item in enumerate(values, start=1):
            if re.match(r"^\s*(?:[ABCD][.．、:]|[（(][ABCD][）)])", item, flags=re.IGNORECASE):
                warnings.append(f"options_latex.{label}[{index}] contains option label")
    question_text = compact_text(compact.get("question_ocr") or compact.get("ocr_text") or "")
    if record.question_type in {"single_choice", "multiple_choice"} and not any(record.options_latex.model_dump().values()):
        warnings.append("choice question has no options after validation")
    if re.search(r"[（(]\s*[）)]|_{2,}|\\_\\_", question_text) and record.question_type == "solution":
        warnings.append("solution type but question text appears to contain a blank")
    return sorted(set(warnings))


def issue_rows(warnings: list[str]) -> list[dict[str, str]]:
    return [{"type": "format_warning", "severity": "warning", "message": warning} for warning in warnings]


def looks_like_standalone_math(text: str, question_type: str) -> bool:
    value = str(text or "").strip()
    if not value or "$" in value or "\n" in value or len(value) > 180:
        return False
    if re.fullmatch(r"[A-D](?:\s*[,，、]\s*[A-D])*", value):
        return True
    if MATH_COMMAND_RE.search(value):
        return True
    if CJK_RE.search(value):
        return False
    if question_type == "fill_blank":
        return bool(re.fullmatch(r"[\dA-Za-z\s.,，;；:：+\-*/^_{}()[\]<>|=\\√π∞]+", value))
    if re.search(r"[=<>_^\\√π∞]", value):
        return bool(re.fullmatch(r"[\dA-Za-z\s.,，;；:：+\-*/^_{}()[\]<>|=\\√π∞]+", value))
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return True
    return False


def wrap_standalone_math_blocks(blocks: Any, question_type: str) -> list[str]:
    wrapped: list[str] = []
    for block in normalize_latex_blocks(blocks):
        text = str(block).strip()
        if looks_like_standalone_math(text, question_type):
            wrapped.append(f"${text}$")
        else:
            wrapped.append(text)
    return wrapped


def option_text_is_math_like(text: str) -> bool:
    value = str(text or "").strip()
    if not value or "$" in value and value.startswith("$") and value.endswith("$"):
        return False
    if VISUAL_PLACEHOLDER_BLOCK_RE.fullmatch(value):
        return False
    cjk_chars = CJK_RE.findall(value)
    if any(char not in ALLOWED_MATH_CONNECTORS for char in cjk_chars):
        return False
    return bool(MATH_COMMAND_RE.search(value) or re.search(r"[A-Za-z0-9]\s*(?:[<>=]|\\leq|\\geq|\\neq)|(?:[<>=]|\\leq|\\geq|\\neq)\s*[A-Za-z0-9]", value))


def normalize_option_math_text(text: str) -> str:
    value = str(text or "").strip()
    if not option_text_is_math_like(value):
        return value
    punctuation = ""
    if value[-1:] in {".", "。", "．"}:
        punctuation = value[-1]
        value = value[:-1].strip()
    value = value.replace("$", "")
    value = re.sub(r"\\text\{\s*([且或并及和与])\s*\}", r"\\text{ \1 }", value)
    protected_texts: list[str] = []

    def protect_text(match: re.Match[str]) -> str:
        protected_texts.append(match.group(0))
        return f"@@TEXT{len(protected_texts) - 1}@@"

    value = re.sub(r"\\text\{[^{}]*\}", protect_text, value)
    for connector in sorted(ALLOWED_MATH_CONNECTORS):
        value = value.replace(connector, rf"\text{{ {connector} }}")
    for index, protected in enumerate(protected_texts):
        value = value.replace(f"@@TEXT{index}@@", protected)
    value = re.sub(r"\s+", " ", value).strip()
    return f"${value}$" + punctuation


def normalize_option_math_blocks(blocks: Any) -> list[str]:
    return [normalize_option_math_text(block) for block in normalize_latex_blocks(blocks)]


def remove_non_option_visual_placeholders(blocks: Any) -> list[str]:
    return [
        block
        for block in normalize_latex_blocks(blocks)
        if not VISUAL_PLACEHOLDER_BLOCK_RE.fullmatch(str(block).strip())
    ]


def one_line_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def table_placeholder_map(source: dict[str, Any]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for item in source.get("non_text_placeholders") or []:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "table":
            continue
        placeholder = str(item.get("placeholder") or "").strip()
        html_text = one_line_text(item.get("html"))
        if placeholder and html_text.lower().startswith("<table"):
            replacements[placeholder] = html_text
    return replacements


def replace_table_placeholders(text: str, replacements: dict[str, str]) -> str:
    out = str(text or "")
    for placeholder, html_text in replacements.items():
        out = out.replace(placeholder, html_text)
    return out


def text_from_blocks(blocks: Any, replacements: dict[str, str]) -> str:
    parts: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "").strip()
        if text:
            parts.append(replace_table_placeholders(text, replacements))
    return "\n".join(parts).strip()


def text_from_answer_items(items: Any, replacements: dict[str, str]) -> str:
    parts: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = text_from_blocks(item.get("blocks") or [], replacements)
        if not text:
            text = replace_table_placeholders(str(item.get("text") or item.get("span_text_excerpt") or "").strip(), replacements)
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts).strip()


def run_mode_for_payload(payload: dict[str, Any]) -> str:
    mode = str(payload.get("pipeline_mode") or payload.get("mode") or "").strip()
    return mode if mode in {"mixed", "paper_plus_answer_file"} else "paper_plus_answer_file"


def minimal_payload_for_model(payload: dict[str, Any]) -> dict[str, Any]:
    qno = int(payload["question_no"])
    mode = run_mode_for_payload(payload)
    replacements = table_placeholder_map(payload)
    boundary_hint = f"只整理第{qno}题。OCR 可能包含上一题末尾或下一题开头，它们只用于边界判断。"
    question_ocr = text_from_blocks(payload.get("question_blocks") or [], replacements)
    answer_ocr = text_from_answer_items(payload.get("answer_items") or [], replacements)
    if mode == "mixed":
        ocr_parts = [part for part in (question_ocr, answer_ocr) if part]
        return {
            "question_no": qno,
            "mode": "mixed",
            "boundary_hint": boundary_hint,
            "ocr_text": "\n\n".join(ocr_parts).strip(),
        }
    compact: dict[str, Any] = {
        "question_no": qno,
        "mode": "paper_plus_answer_file",
        "boundary_hint": boundary_hint,
        "question_ocr": question_ocr,
    }
    if answer_ocr:
        compact["answer_ocr"] = answer_ocr
    return compact


def prepare_step3_question_crop_images(payload: dict[str, Any], runs_root: Path, out_dir: Path) -> list[Path]:
    run_id = str(payload.get("run_id") or "")
    qno = int(payload["question_no"])
    labels = question_surface_crop_labels(payload)
    if not labels:
        raise ValueError(f"Step2 produced no question range labels for {run_id} Q{qno}")
    paths = crop_labels_to_images(
        run_id=run_id,
        qno=qno,
        labels=[str(label) for label in labels if str(label)],
        run_block_dir=runs_root / f"{run_id}_raw_units",
        crop_dir=out_dir / "per_question" / "step3_question_crops",
        filename_role="question_surface",
        margin=0,
        split_shared_bboxes=False,
    )
    if not paths:
        raise ValueError(f"Step2 question range labels did not produce crop images for {run_id} Q{qno}")
    return paths


def question_surface_crop_labels(payload: dict[str, Any]) -> list[str]:
    return unique_str([str(label) for label in payload.get("step2_question_range_labels") or [] if str(label)])


def build_step3_messages(compact: dict[str, Any], question_crop_images: list[Path]) -> list[dict[str, Any]]:
    if not question_crop_images:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT + json.dumps(compact, ensure_ascii=False, indent=2)},
        ]
    qno = int(compact["question_no"])
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"下面是 Step2 根据范围检测为第{qno}题裁出的题面图。"
                "题面识别以图片为准，OCR 文本只作为补充；如果图片边缘含相邻题，只整理本题。"
            ),
        }
    ]
    for index, image_path in enumerate(question_crop_images, start=1):
        content.append({"type": "text", "text": f"第{qno}题题面裁剪图 {index}："})
        content.append({"type": "image_url", "image_url": {"url": data_uri(image_path)}})
    content.append({"type": "text", "text": USER_PROMPT + json.dumps(compact, ensure_ascii=False, indent=2)})
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def option_asset_placeholders_from_source(source: dict[str, Any]) -> dict[str, dict[str, str]]:
    placeholders: dict[str, dict[str, str]] = {}
    for asset in source.get("visual_assets") or []:
        if not isinstance(asset, dict) or str(asset.get("role") or "") != "option_figure":
            continue
        option_label = str(asset.get("option_label") or "").strip().upper()
        label = str(asset.get("label") or "").strip()
        if option_label not in {"A", "B", "C", "D"} or not label:
            continue
        placeholder = str(asset.get("placeholder") or "").strip()
        placeholders[option_label] = {
            "label": label,
            **({"placeholder": placeholder} if placeholder else {}),
        }
    return placeholders


def replace_table_placeholders_in_record(record: dict[str, Any], source: dict[str, Any]) -> None:
    replacements = table_placeholder_map(source)
    if not replacements:
        return
    for field_name in ("stem_latex", "answer_latex", "analysis_latex", "rubric_latex"):
        record[field_name] = [
            replace_table_placeholders(str(block), replacements)
            for block in normalize_latex_blocks(record.get(field_name))
        ]
    options = record.get("options_latex") if isinstance(record.get("options_latex"), dict) else {}
    record["options_latex"] = {
        key: [
            replace_table_placeholders(str(block), replacements)
            for block in normalize_latex_blocks(options.get(key) or [])
        ]
        for key in ("A", "B", "C", "D")
        if normalize_latex_blocks(options.get(key) or [])
    }


def normalize_schema_record(record: Step3Record, source: dict[str, Any], model: str, elapsed: float, warnings: list[str]) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    options = payload.get("options_latex") if isinstance(payload.get("options_latex"), dict) else {}
    normalized_options = normalize_options_latex(options)
    normalized = {
        "schema_version": "step3_json_schema_v1",
        "question_no": int(source["question_no"]),
        "question_type": record.question_type,
        "stem_latex": strip_top_question_no(normalize_latex_blocks(payload.get("stem_latex")), int(source["question_no"])),
        "options_latex": normalized_options,
        "answer_latex": normalize_latex_blocks(payload.get("answer_latex")),
        "analysis_latex": normalize_latex_blocks(payload.get("analysis_latex")),
        "rubric_latex": normalize_latex_blocks(payload.get("rubric_latex")),
        "figure_labels": list(source.get("figure_labels") or []),
        "table_labels": list(source.get("table_labels") or []),
        "visual_assets": list(source.get("visual_assets") or []),
        "option_asset_placeholders": option_asset_placeholders_from_source(source),
        "source_question_labels": list(source.get("source_question_labels") or []),
        "source_answer_labels": list(source.get("source_answer_labels") or []),
        "issues": normalize_issues(payload.get("issues")) + issue_rows(warnings),
        "model": model,
        "standardize_elapsed_seconds": round(elapsed, 3),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    replace_table_placeholders_in_record(normalized, source)
    apply_visual_asset_policy(normalized, source)
    cleanup_unknown_table_placeholders(normalized, source)
    normalize_blank_marks(normalized)
    validate_record(normalized, source)
    for field_name in ("stem_latex", "answer_latex", "analysis_latex", "rubric_latex"):
        normalized[field_name] = remove_non_option_visual_placeholders(normalized.get(field_name))
    normalized["answer_latex"] = wrap_standalone_math_blocks(normalized.get("answer_latex"), record.question_type)
    options = normalized.get("options_latex") if isinstance(normalized.get("options_latex"), dict) else {}
    normalized["options_latex"] = {
        key: normalize_option_math_blocks(wrap_standalone_math_blocks(options.get(key) or [], record.question_type))
        for key in ("A", "B", "C", "D")
    }
    return normalized


def run_question(
    payload: dict[str, Any],
    out_dir: Path,
    runs_root: Path,
    model: str,
    timeout: int,
    token: str,
    force: bool,
    enable_thinking: bool,
) -> dict[str, Any]:
    qno = int(payload["question_no"])
    per_dir = out_dir / "per_question"
    input_path = per_dir / f"q{qno:02d}_input.json"
    source_path = per_dir / f"q{qno:02d}_source_payload.json"
    raw_path = per_dir / f"q{qno:02d}_raw.json.txt"
    validated_path = per_dir / f"q{qno:02d}_validated.json"
    parsed_path = per_dir / f"q{qno:02d}.json"

    compact = minimal_payload_for_model(payload)
    audit_input = dict(compact)
    audit_input["step2_question_range_labels"] = question_surface_crop_labels(payload)
    write_json(input_path, audit_input)
    write_json(source_path, payload)
    started = time.monotonic()
    try:
        question_crop_images = prepare_step3_question_crop_images(payload, runs_root, out_dir)
        audit_input["question_crop_images"] = [str(path.resolve()) for path in question_crop_images]
        write_json(input_path, audit_input)
        if parsed_path.exists() and validated_path.exists() and not force:
            validated = Step3Record.model_validate(read_json(validated_path, {}))
            warnings = content_warnings(validated, compact)
            previous = read_json(parsed_path, {})
            elapsed = float(previous.get("standardize_elapsed_seconds") or 0.0) if isinstance(previous, dict) else 0.0
            record = normalize_schema_record(validated, payload, model=model, elapsed=elapsed, warnings=warnings)
            write_json(parsed_path, record)
            return {
                "question_no": qno,
                "status": "cached_refreshed_with_warnings" if warnings else "cached_refreshed",
                "question_type": record.get("question_type"),
                "elapsed_seconds": record.get("standardize_elapsed_seconds"),
                "warning_count": len(warnings),
                "warnings": warnings,
                "record": record,
            }

        messages = build_step3_messages(compact, question_crop_images)
        raw = call_json_schema(messages, model=model, timeout=timeout, token=token, enable_thinking=enable_thinking)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(raw, encoding="utf-8", newline="\n")
        parsed = json.loads(raw)
        validated = Step3Record.model_validate(parsed)
        if int(validated.question_no) != qno:
            raise ValueError(f"question_no mismatch: expected {qno}, got {validated.question_no}")
        warnings = content_warnings(validated, compact)
        write_json(validated_path, validated.model_dump(mode="json"))
        record = normalize_schema_record(validated, payload, model=model, elapsed=time.monotonic() - started, warnings=warnings)
        write_json(parsed_path, record)
        return {
            "question_no": qno,
            "status": "ok_with_warnings" if warnings else "ok",
            "question_type": record.get("question_type"),
            "elapsed_seconds": record.get("standardize_elapsed_seconds"),
            "warning_count": len(warnings),
            "warnings": warnings,
            "record": record,
        }
    except (ValidationError, json.JSONDecodeError, Exception) as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        error = f"{type(exc).__name__}: {exc}"
        record = error_record(payload, model=model, message=error, elapsed=elapsed)
        record["schema_version"] = "step3_json_schema_v1"
        write_json(parsed_path, record)
        return {
            "question_no": qno,
            "status": "error",
            "elapsed_seconds": round(elapsed, 3),
            "error": error,
            "traceback": traceback.format_exc(),
            "record": record,
        }


def estimate_task_weight(payload: dict[str, Any]) -> int:
    compact = minimal_payload_for_model(payload)
    text = "\n".join(str(compact.get(key) or "") for key in ("ocr_text", "question_ocr", "answer_ocr"))
    return len(text)


def summarize(run_id: str, records: list[dict[str, Any]], results: list[dict[str, Any]], out_dir: Path, model: str) -> dict[str, Any]:
    summary = summarize_records(run_id, records, out_dir)
    status_counts: dict[str, int] = {}
    warning_counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        for warning in result.get("warnings") or []:
            warning_counts[str(warning)] = warning_counts.get(str(warning), 0) + 1
    summary.update(
        {
            "model": model,
            "standardizer": "json_schema_pydantic",
            "status_counts": status_counts,
            "warning_counts": warning_counts,
            "results": [
                {key: value for key, value in result.items() if key not in {"record", "traceback"}}
                for result in sorted(results, key=lambda item: int(item.get("question_no") or 0))
            ],
        }
    )
    write_json(out_dir / "summary.json", summary)
    write_json(out_dir / "question_bank.json", records)
    write_jsonl(out_dir / "question_bank.jsonl", records)
    return summary


def run_one_run(
    run_id: str,
    runs_root: Path,
    output_root: Path,
    model: str,
    timeout: int,
    max_workers: int,
    force: bool,
    token: str,
    question_numbers: set[int] | None = None,
    enable_thinking: bool = False,
) -> dict[str, Any]:
    out_dir = output_root / run_id
    payloads = build_payloads_for_run(run_id, runs_root)
    pipeline_summary = read_json(runs_root / f"{run_id}_raw_units" / "pipeline_summary.json", {})
    pipeline_mode = str(pipeline_summary.get("mode") or "paper_plus_answer_file")
    for payload in payloads:
        payload["pipeline_mode"] = pipeline_mode
    if question_numbers:
        payloads = [payload for payload in payloads if int(payload["question_no"]) in question_numbers]
    payloads.sort(key=lambda payload: (-estimate_task_weight(payload), int(payload["question_no"])))
    write_json(
        out_dir / "queue_plan.json",
        {
            "strategy": "longest_question_answer_first",
            "items": [
                {"question_no": int(payload["question_no"]), "weight": estimate_task_weight(payload)}
                for payload in payloads
            ],
        },
    )
    print(
        json.dumps(
            {
                "event": "start_step3_json_schema",
                "run_id": run_id,
                "question_count": len(payloads),
                "max_workers": max_workers,
                "queue": "longest_question_answer_first",
                "question_numbers": sorted(question_numbers) if question_numbers else None,
                "enable_thinking": enable_thinking,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    results: list[dict[str, Any]] = []
    records_by_qno: dict[int, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_question, payload, out_dir, runs_root, model, timeout, token, force, enable_thinking): int(payload["question_no"])
            for payload in payloads
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            if isinstance(result.get("record"), dict):
                records_by_qno[int(result["question_no"])] = result["record"]
            print(
                json.dumps(
                    {
                        "event": "done_step3_json_schema",
                        "run_id": run_id,
                        "question_no": result.get("question_no"),
                        "status": result.get("status"),
                        "elapsed_seconds": result.get("elapsed_seconds"),
                        "warning_count": result.get("warning_count"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    records = [records_by_qno[qno] for qno in sorted(records_by_qno)]
    summary = summarize(run_id, records, results, out_dir, model)
    print(
        json.dumps(
            {
                "event": "summary_step3_json_schema",
                "run_id": run_id,
                "question_count": summary.get("question_count"),
                "status_counts": summary.get("status_counts"),
                "question_type_counts": summary.get("question_type_counts"),
                "summary_path": str(out_dir / "summary.json"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Step3 question-bank standardization with provider JSON Schema and Pydantic validation.")
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--tpm-limit", type=int, default=None, help="Local LLM tokens-per-minute limit. 0 disables.")
    parser.add_argument("--token-estimator-model", default=None, help="Tokenizer/processor model. Defaults to --model.")
    parser.add_argument("--image-token-mode", choices=["auto", "processor", "formula"], default="auto")
    parser.add_argument("--tpm-output-reserve", type=int, default=DEFAULT_OUTPUT_RESERVE_TOKENS)
    parser.add_argument("--token-budget-log", type=Path, default=None)
    parser.add_argument("--token-budget-verbose", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--question-no", action="append", type=int, help="Optional question number filter. Repeatable.")
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=False)
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
    token = load_token()
    question_numbers = set(args.question_no or [])
    summaries = [
        run_one_run(
            run_id=run_id,
            runs_root=args.runs_root,
            output_root=args.output_root,
            model=args.model,
            timeout=args.timeout,
            max_workers=args.max_workers,
            force=args.force,
            token=token,
            question_numbers=question_numbers or None,
            enable_thinking=args.enable_thinking,
        )
        for run_id in args.run_id
    ]
    write_json(args.output_root / "batch_summary.json", {"runs": summaries, "model": args.model})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
