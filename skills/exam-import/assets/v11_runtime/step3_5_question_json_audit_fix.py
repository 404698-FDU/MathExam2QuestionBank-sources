#!/usr/bin/env python3
"""Audit Step3 math wrapping and optionally ask an LLM to fix only formatting.

This is an experimental Step3.5. It reads Step3 validated records, detects
LaTeX/math-like fragments outside math delimiters, and sends only flagged
questions to a small formatting model.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from common_io import read_json, write_json, write_jsonl, write_text
from common_llm import load_token, post_chat_completion
from common_math_text import split_math_segments
from common_token_budget import DEFAULT_OUTPUT_RESERVE_TOKENS, configure_token_budget_env
from step3_schema import STRICT_JSON_SCHEMA as JSON_SCHEMA
from step3_schema import StrictStep3Record as Step3Record


WORKTREE_ROOT = Path(__file__).resolve().parent

DEFAULT_QB_ROOT = WORKTREE_ROOT / "runs_question_bank"
DEFAULT_OUTPUT_ROOT = WORKTREE_ROOT / "reviews_step3_5_latex_audit"
DEFAULT_MODEL = "Qwen/Qwen3.6-35B-A3B"

STEP3_RECORD_TOOL_SCHEMA = {
    "type": "object",
    "required": [
        "schema_version",
        "question_no",
        "question_type",
        "stem_latex",
        "options_latex",
        "answer_latex",
        "analysis_latex",
        "rubric_latex",
        "issues",
    ],
    "properties": {
        "schema_version": {"type": "string", "enum": ["step3_json_schema_v1"]},
        "question_no": {"type": "integer"},
        "question_type": {"type": "string", "enum": ["fill_blank", "single_choice", "multiple_choice", "solution", "unknown"]},
        "stem_latex": {"type": "array", "items": {"type": "string"}},
        "options_latex": {
            "type": "object",
            "required": ["A", "B", "C", "D"],
            "properties": {
                "A": {"type": "array", "items": {"type": "string"}},
                "B": {"type": "array", "items": {"type": "string"}},
                "C": {"type": "array", "items": {"type": "string"}},
                "D": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "answer_latex": {"type": "array", "items": {"type": "string"}},
        "analysis_latex": {"type": "array", "items": {"type": "string"}},
        "rubric_latex": {"type": "array", "items": {"type": "string"}},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "severity", "message"],
                "properties": {
                    "type": {"type": "string"},
                    "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                    "message": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def normalize_worker_models(primary_model: str, worker_models: list[str] | None) -> list[str]:
    models = [str(model).strip() for model in (worker_models or []) if str(model).strip()]
    if not models:
        models = [primary_model]
    return list(dict.fromkeys(models))


def multi_model_name(worker_models: list[str]) -> str:
    if len(worker_models) == 1:
        return worker_models[0]
    return "multi_model[" + ",".join(worker_models) + "]"

MATH_COMMAND_RE = re.compile(
    r"\\(?:frac|sqrt|vec|overrightarrow|cdot|times|leq|geq|neq|in|notin|mathbb|mathbf|mathrm|text|sin|cos|tan|log|ln|alpha|beta|gamma|pi|theta|Delta|left|right)\b"
)
MATH_RELATION_RE = re.compile(
    r"(?:[A-Za-z](?:_\{[^{}]+\})?|\d+(?:\.\d+)?|[)}])\s*(?:[<>]=?|=|\\leq|\\geq|\\neq)\s*(?:[A-Za-z](?:_\{[^{}]+\})?|\d+(?:\.\d+)?|\\[A-Za-z]+|[({])"
)
SUBSUP_RE = re.compile(r"[A-Za-z0-9)}]\s*[_^]\s*(?:\{|[A-Za-z0-9])")
PLACEHOLDER_RE = re.compile(r"^<(?:blank|choice_blank|image|chart|figure|table)\d*>$", re.IGNORECASE)
CHOICE_BLANK_RE = re.compile(r"<choice_blank>", re.IGNORECASE)
CHOICE_BRACKET_RE = re.compile(r"(?:（\s*）|\(\s*\))")
MATH_CONTINUITY_CONNECTOR_RE = re.compile(
    r"^[\s，,、]*(?:使得|满足|其中|且|并且|若|当|对任意|任意|存在|不存在)[\s，,、]*$"
)
MATH_CONDITION_CONTEXT_RE = re.compile(
    r"(?:\\left\s*\\\{|\\right\s*\\\}|\\mid|\\left\s*\||\\right\s*\||"
    r"\\text\{[^{}]*(?:存在|不存在|满足|使得)[^{}]*\})"
)
LEFT_TOKEN_RE = re.compile(r"\\left\b")
RIGHT_TOKEN_RE = re.compile(r"\\right\b")
HTML_ENTITY_RE = re.compile(r"&(?:gt|lt|amp|ge|le|nbsp|quot|apos|#\d+|#x[0-9A-Fa-f]+);")

SYSTEM_PROMPT = (
    "你是数学题库 LaTeX 格式修复器。你只修复数学表达式的 LaTeX 包裹和行内格式，"
    "不得解题、不得改写题意、不得增删内容、不得重新分类字段。"
)

TOOL_CALLING_SYSTEM_PROMPT = (
    "/no_think\n\n"
    "你是数学题库 LaTeX 格式修复器。\n"
    "本次任务必须通过调用工具 submit_step3_5_record 完成。\n"
    "不得在普通回复正文中写 JSON、Markdown、代码块、解释或分析过程。\n"
    "不得使用 message.content 提交结果。\n\n"
    "你只修复数学表达式的 LaTeX 包裹和行内格式。"
    "不得解题、不得改写题意、不得增删内容、不得重新分类字段。"
)

USER_PROMPT = """\
/no_think

下面是一道 Step3 已经结构化好的数学题 JSON，以及脚本审计出的 LaTeX 包裹问题。
你的任务只是在原字段内修复格式：
- 数学表达式必须放入 $...$，展示数学放入 $$...$$。
- 如果一整段选项几乎全是数学条件，例如 0 < a < 1 且 x > 1/2，则把整段包成一个行内数学块，并把中文连接词写成 \\text{ 且 }、\\text{ 或 } 等。
- 如果审计原因包含 math_condition_split_across_text_connector，说明一个集合、条件或分段数学表达被拆成了 `$...$ 中文连接词 `$...$`。请把相邻数学片段和连接词合并为一个数学表达式，连接词写入 `\\text{...}`，例如把 `$A$，使得 `$B$` 修为 `$A\\text{，使得 }B$`。
- 合并这类片段时只修复 LaTeX 分段，不得增删原有数学条件；只有为配平括号/定界符所必需时，才可以去掉不必要的 `\\left`、`\\right`。
- 如果审计原因包含 left_right_count_mismatch，必须配平或移除不必要的 `\\left`、`\\right`。集合的条件分隔符使用 `\\mid`，不要使用未成对的 `\\left|` 或 `\\right|`；绝对值可以写成 `|...|`，或写成完整配对的 `\\left|...\\right|`。
- 例如集合应写为 `$U=\\{x\\mid |x|=1\\}$` 或 `$U=\\left\\{x\\mid |x|=1\\right\\}$`，不要写成 `$\\left\\{x\\left|\\left|x\\right|=1\\right\\}$`。
- 如果审计原因包含 duplicate_choice_blank 或 missing_choice_blank，选择题题干必须且只能有一个 `<choice_blank>`。
- 多个 `<choice_blank>` 时，保留题干问句中的作答位置，例如“正确的是/不可能发生的是/应选的是”后面的 `<choice_blank>`，删除单独成行或重复出现的 `<choice_blank>`；不得删除选项、答案或题干文字。
- 若选择题原文作答位是 `（）` 或 `( )`，统一改成 `<choice_blank>`。
- 如果只有局部数学，例如中文句子中的变量、公式、区间，则只包局部数学，不要把整句中文都放入数学环境。
- 字段中不得保留 HTML 实体，例如 &gt;、&lt;、&amp;。在 LaTeX 数学中应改为直接关系符号或 LaTeX 命令，例如 `$t &gt; 2$` 必须修为 `$t > 2$` 或 `$t \\gt 2$`。
- 已经正确包裹的数学不要重复包裹。
- 保留 <blank>、<choice_blank>、<image01>、<table01> 等占位符。
- 保留 HTML 表格 <table>...</table> 结构；只处理单元格文本中的数学包裹。
- 不要改变 question_no、question_type、字段结构、选项键、答案内容、解析内容、issues 含义。
- 不得删除或改写原有中文、英文、数字和标点；修复 HTML 实体时只把实体替换为等价字符，例如 &gt; 只改为 >。
- 不要新增解释，不要输出 Markdown，只输出满足 JSON Schema 的 JSON object。

输入 JSON：
"""

TOOL_CALLING_USER_PROMPT = """\
下面是一道 Step3 已经结构化好的数学题记录，以及脚本审计出的 LaTeX 包裹问题。

任务边界：
- 只在原字段内修复格式。
- 数学表达式必须放入 $...$，展示数学放入 $$...$$。
- 如果一整段选项几乎全是数学条件，例如 0 < a < 1 且 x > 1/2，则把整段包成一个行内数学块，并把中文连接词写成 \\text{ 且 }、\\text{ 或 } 等。
- 如果审计原因包含 math_condition_split_across_text_connector，说明一个集合、条件或分段数学表达被拆成了 `$...$ 中文连接词 `$...$`。请把相邻数学片段和连接词合并为一个数学表达式，连接词写入 `\\text{...}`。
- 合并这类片段时只修复 LaTeX 分段，不得增删原有数学条件；只有为配平括号/定界符所必需时，才可以去掉不必要的 `\\left`、`\\right`。
- 如果审计原因包含 left_right_count_mismatch，必须配平或移除不必要的 `\\left`、`\\right`。集合的条件分隔符使用 `\\mid`，不要使用未成对的 `\\left|` 或 `\\right|`；绝对值可以写成 `|...|`，或写成完整配对的 `\\left|...\\right|`。
- 例如集合应写为 `$U=\\{x\\mid |x|=1\\}$` 或 `$U=\\left\\{x\\mid |x|=1\\right\\}$`，不要写成 `$\\left\\{x\\left|\\left|x\\right|=1\\right\\}$`。
- 如果审计原因包含 duplicate_choice_blank 或 missing_choice_blank，选择题题干必须且只能有一个 `<choice_blank>`。
- 多个 `<choice_blank>` 时，保留题干问句中的作答位置，例如“正确的是/不可能发生的是/应选的是”后面的 `<choice_blank>`，删除单独成行或重复出现的 `<choice_blank>`；不得删除选项、答案或题干文字。
- 若选择题原文作答位是 `（）` 或 `( )`，统一改成 `<choice_blank>`。
- 如果只有局部数学，例如中文句子中的变量、公式、区间，则只包局部数学，不要把整句中文都放入数学环境。
- 字段中不得保留 HTML 实体，例如 &gt;、&lt;、&amp;。在 LaTeX 数学中应改为直接关系符号或 LaTeX 命令，例如 `$t &gt; 2$` 必须修为 `$t > 2$` 或 `$t \\gt 2$`。
- 已经正确包裹的数学不要重复包裹。
- 保留 <blank>、<choice_blank>、<image01>、<table01> 等占位符。
- 保留 HTML 表格 <table>...</table> 结构；只处理单元格文本中的数学包裹。
- 不要改变 question_no、question_type、字段结构、选项键、答案内容、解析内容、issues 含义。
- 不得删除或改写原有中文、英文、数字和标点；修复 HTML 实体时只把实体替换为等价字符，例如 &gt; 只改为 >。

必须调用工具 submit_step3_5_record，并在工具参数中填写修复后的完整单题记录。
普通正文保持为空。

输入数据：
"""


def audit_math_continuity(text: str) -> list[str]:
    reasons: list[str] = []
    segments = split_math_segments(text)
    for index in range(0, max(0, len(segments) - 2)):
        left_is_math, left = segments[index]
        middle_is_math, middle = segments[index + 1]
        right_is_math, right = segments[index + 2]
        if not left_is_math or middle_is_math or not right_is_math:
            continue
        if not MATH_CONTINUITY_CONNECTOR_RE.fullmatch(middle):
            continue
        if MATH_CONDITION_CONTEXT_RE.search(left) or MATH_CONDITION_CONTEXT_RE.search(right):
            reasons.append("math_condition_split_across_text_connector")
    return sorted(set(reasons))


def audit_math_segment(segment: str) -> list[str]:
    reasons: list[str] = []
    left_count = len(LEFT_TOKEN_RE.findall(segment))
    right_count = len(RIGHT_TOKEN_RE.findall(segment))
    if left_count != right_count:
        reasons.append("left_right_count_mismatch")
    return reasons


def audit_text(text: str) -> list[str]:
    reasons: list[str] = []
    stripped = str(text or "").strip()
    if not stripped or PLACEHOLDER_RE.fullmatch(stripped):
        return reasons
    if HTML_ENTITY_RE.search(stripped):
        reasons.append("html_entity_in_latex_text")
    reasons.extend(audit_math_continuity(stripped))
    for is_math, segment in split_math_segments(stripped):
        if is_math:
            reasons.extend(audit_math_segment(segment))
            continue
        outside = re.sub(r"<[^>]+>", " ", segment)
        if MATH_COMMAND_RE.search(outside):
            reasons.append("latex_command_outside_math")
        if MATH_RELATION_RE.search(outside):
            reasons.append("math_relation_outside_math")
        if SUBSUP_RE.search(outside):
            reasons.append("subsup_outside_math")
    return sorted(set(reasons))


def html_entity_finding_count(findings: list[dict[str, Any]]) -> int:
    return sum(1 for item in findings if "html_entity_in_latex_text" in set(item.get("reasons") or []))


def iter_record_blocks(record: dict[str, Any], fields: set[str]) -> list[tuple[str, str | None, int, str]]:
    rows: list[tuple[str, str | None, int, str]] = []
    for field_name in ("stem_latex", "answer_latex", "analysis_latex", "rubric_latex"):
        if field_name not in fields:
            continue
        for index, value in enumerate(record.get(field_name) or [], start=1):
            rows.append((field_name, None, index, str(value)))
    if "options_latex" in fields:
        options = record.get("options_latex") if isinstance(record.get("options_latex"), dict) else {}
        for key in ("A", "B", "C", "D"):
            for index, value in enumerate(options.get(key) or [], start=1):
                rows.append(("options_latex", key, index, str(value)))
    return rows


def audit_record(record: dict[str, Any], fields: set[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    qno = int(record.get("question_no") or 0)
    for field_name, option_key, index, text in iter_record_blocks(record, fields):
        reasons = audit_text(text)
        if not reasons:
            continue
        findings.append(
            {
                "question_no": qno,
                "field": field_name,
                "option_key": option_key,
                "index": index,
                "reasons": reasons,
                "text": text,
            }
        )
    if "stem_latex" in fields and record.get("question_type") in {"single_choice", "multiple_choice"}:
        stem_items = [str(item) for item in record.get("stem_latex") or []]
        choice_blank_count = sum(len(CHOICE_BLANK_RE.findall(item)) + len(CHOICE_BRACKET_RE.findall(item)) for item in stem_items)
        if choice_blank_count != 1:
            findings.append(
                {
                    "question_no": qno,
                    "field": "stem_latex",
                    "option_key": None,
                    "index": 0,
                    "reasons": ["missing_choice_blank" if choice_blank_count == 0 else "duplicate_choice_blank"],
                    "text": "\n".join(stem_items),
                }
            )
    return findings


def can_fix_findings_without_llm(findings: list[dict[str, Any]]) -> bool:
    if not findings:
        return False
    return all(set(item.get("reasons") or []) == {"html_entity_in_latex_text"} for item in findings)


def unescape_latex_entities(record: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    fixed = copy.deepcopy(record)
    for field_name in ("stem_latex", "answer_latex", "analysis_latex", "rubric_latex"):
        if field_name not in fields:
            continue
        fixed[field_name] = [html.unescape(str(value)) for value in fixed.get(field_name) or []]
    if "options_latex" in fields:
        options = fixed.get("options_latex") if isinstance(fixed.get("options_latex"), dict) else {}
        fixed["options_latex"] = {
            key: [html.unescape(str(value)) for value in values]
            for key, values in options.items()
        }
    return fixed


def strip_math_delimiters(text: str) -> str:
    text = html.unescape(str(text or ""))
    text = text.replace(r"\(", "").replace(r"\)", "").replace(r"\[", "").replace(r"\]", "")
    text = text.replace("$$", "").replace("$", "")
    text = re.sub(r"\\(?:left|right)\s*", "", text)
    text = text.replace(r"\mid", "|").replace(r"\{", "{").replace(r"\}", "}")
    text = re.sub(r"\\(?:,|;|:|!|quad|qquad)\s*", "", text)
    text = text.replace(r"\ ", "")
    text = CHOICE_BLANK_RE.sub("", text)
    text = CHOICE_BRACKET_RE.sub("", text)
    return text


def content_fingerprint(record: dict[str, Any], fields: set[str]) -> str:
    parts = []
    for field_name, option_key, _index, text in iter_record_blocks(record, fields):
        label = f"{field_name}.{option_key or ''}"
        value = strip_math_delimiters(text)
        value = re.sub(r"\\text\{\s*([^{}]*?)\s*\}", r"\1", value)
        value = re.sub(r"\s+", "", value)
        if field_name == "stem_latex" and not value:
            continue
        parts.append(f"{label}:{value}")
    return "|".join(parts)


def call_formatter(
    record: dict[str, Any],
    findings: list[dict[str, Any]],
    model: str,
    timeout: int,
    token: str,
    enable_thinking: bool,
    structured_output: str,
) -> tuple[dict[str, Any], str, float]:
    if structured_output == "tool_calling":
        system_prompt = TOOL_CALLING_SYSTEM_PROMPT
        user_prompt = TOOL_CALLING_USER_PROMPT
    else:
        system_prompt = SYSTEM_PROMPT
        user_prompt = USER_PROMPT
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": user_prompt
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
                    "description": "提交 Step3.5 修复后的完整单题题库 JSON 记录。",
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
                raise RuntimeError(f"Step3.5 model called unexpected tool {actual_name!r}")
            raw = tool_call["function"]["arguments"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Step3.5 model returned no tool call: {json.dumps(payload, ensure_ascii=False)[:1200]}") from exc
    else:
        raw = payload["choices"][0]["message"]["content"]
    parsed = json.loads(raw)
    validated = Step3Record.model_validate(parsed).model_dump(mode="json")
    return validated, raw, time.monotonic() - started


def load_records(run_dir: Path, question_numbers: set[int] | None, source_stage: str) -> list[dict[str, Any]]:
    if source_stage == "question_bank":
        payload = read_json(run_dir / "question_bank.json", [])
        records = payload.get("questions", []) if isinstance(payload, dict) else payload
        if question_numbers:
            records = [record for record in records if int(record.get("question_no") or 0) in question_numbers]
        return sorted(records, key=lambda record: int(record.get("question_no") or 0))

    per_dir = run_dir / "per_question"
    if question_numbers:
        paths = [per_dir / f"q{qno:02d}_validated.json" for qno in sorted(question_numbers)]
    else:
        paths = sorted(per_dir.glob("q*_validated.json"))
    records = [read_json(path, {}) for path in paths if path.exists()]
    return sorted(records, key=lambda record: int(record.get("question_no") or 0))


def load_question_bank(run_dir: Path) -> list[dict[str, Any]]:
    payload = read_json(run_dir / "question_bank.json", [])
    records = payload.get("questions", []) if isinstance(payload, dict) else payload
    return sorted(records, key=lambda record: int(record.get("question_no") or 0))


def field_set(value: str) -> set[str]:
    if value == "question_surface":
        return {"stem_latex", "options_latex"}
    if value == "all":
        return {"stem_latex", "options_latex", "answer_latex", "analysis_latex", "rubric_latex"}
    return {item.strip() for item in value.split(",") if item.strip()}


def applicable_result_qnos(results: list[dict[str, Any]]) -> tuple[set[int], list[dict[str, Any]]]:
    applied: set[int] = set()
    rejected: list[dict[str, Any]] = []
    for result in results:
        qno = int(result.get("question_no") or 0)
        status = str(result.get("status") or "")
        if not status.startswith("fixed"):
            continue
        reasons: list[str] = []
        if int(result.get("after_finding_count") or 0) != 0:
            reasons.append("remaining_audit_findings")
        if bool(result.get("changed_content_fingerprint")):
            reasons.append("content_fingerprint_changed")
        html_entity_reduced = int(result.get("after_html_entity_finding_count") or 0) < int(
            result.get("before_html_entity_finding_count") or 0
        )
        if reasons == ["remaining_audit_findings"] and html_entity_reduced:
            applied.add(qno)
            continue
        if reasons:
            rejected.append({"question_no": qno, "reasons": reasons})
        else:
            applied.add(qno)
    return applied, rejected


def merge_fixed_fields(
    base_records: list[dict[str, Any]],
    fixed_records: list[dict[str, Any]],
    results: list[dict[str, Any]],
    fields: set[str],
) -> tuple[list[dict[str, Any]], list[int], list[dict[str, Any]]]:
    applied_qnos, rejected = applicable_result_qnos(results)
    fixed_by_qno = {int(record.get("question_no") or 0): record for record in fixed_records}
    merged: list[dict[str, Any]] = []
    actually_changed: list[int] = []
    for record in base_records:
        qno = int(record.get("question_no") or 0)
        fixed = fixed_by_qno.get(qno)
        if qno not in applied_qnos or not fixed:
            merged.append(record)
            continue
        out = dict(record)
        changed = False
        for field in sorted(fields):
            if field in fixed and out.get(field) != fixed.get(field):
                out[field] = fixed.get(field)
                changed = True
        if changed:
            actually_changed.append(qno)
        merged.append(out)
    return merged, actually_changed, rejected


def write_back_question_bank(run_dir: Path, records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    qb_path = run_dir / "question_bank.json"
    backup_path = run_dir / "question_bank.before_step3_5_latex_audit.json"
    if qb_path.exists() and not backup_path.exists():
        backup_path.write_text(qb_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    write_json(qb_path, records)
    write_jsonl(run_dir / "question_bank.jsonl", records)
    write_json(run_dir / "step3_5_latex_audit_summary.json", summary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Step3 LaTeX wrapping and optionally use an LLM to reformat flagged records.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--qb-root", type=Path, default=DEFAULT_QB_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--worker-model", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--tpm-limit", type=int, default=None, help="Local LLM tokens-per-minute limit. 0 disables.")
    parser.add_argument("--token-estimator-model", default=None, help="Tokenizer/processor model. Defaults to --model.")
    parser.add_argument("--image-token-mode", choices=["auto", "processor", "formula"], default="auto")
    parser.add_argument("--tpm-output-reserve", type=int, default=DEFAULT_OUTPUT_RESERVE_TOKENS)
    parser.add_argument("--token-budget-log", type=Path, default=None)
    parser.add_argument("--token-budget-verbose", action="store_true")
    parser.add_argument("--question-no", action="append", type=int)
    parser.add_argument("--source-stage", choices=["validated", "question_bank"], default="validated")
    parser.add_argument("--fields", default="all", help="question_surface, all, or comma-separated field names.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--structured-output", choices=["json_schema", "tool_calling"], default="tool_calling")
    parser.add_argument("--merge-base-qb-root", type=Path, default=None, help="Question-bank root to merge fixed fields into. Defaults to --qb-root.")
    parser.add_argument("--write-back", action="store_true", help="Write accepted fixed fields back to the merge-base question_bank.json.")
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
    flagged = [record for record in records if before_findings_by_qno.get(int(record["question_no"]))]

    write_json(
        out_dir / "audit_before.json",
        {
            "run_id": args.run_id,
            "source_stage": args.source_stage,
            "fields": sorted(fields),
            "finding_count": sum(len(items) for items in before_findings_by_qno.values()),
            "flagged_questions": [int(record["question_no"]) for record in flagged],
            "findings": [item for qno in sorted(before_findings_by_qno) for item in before_findings_by_qno[qno]],
        },
    )

    results: list[dict[str, Any]] = []
    fixed_by_qno: dict[int, dict[str, Any]] = {}
    token = None if args.dry_run or not flagged else load_token()
    flagged_by_qno = {int(record["question_no"]): index for index, record in enumerate(flagged)}
    model_by_qno = {
        int(record["question_no"]): worker_models[index % len(worker_models)]
        for index, record in enumerate(flagged)
    }

    for record in records:
        qno = int(record["question_no"])
        findings = before_findings_by_qno.get(qno) or []
        if not findings:
            fixed_by_qno[qno] = record
            results.append({"question_no": qno, "status": "clean", "before_finding_count": 0})
            continue
        per_dir = out_dir / "per_question"
        write_json(per_dir / f"q{qno:02d}_before.json", record)
        write_json(per_dir / f"q{qno:02d}_findings_before.json", findings)
        if args.dry_run:
            fixed_by_qno[qno] = record
            results.append(
                {
                    "question_no": qno,
                    "status": "flagged_dry_run",
                    "before_finding_count": len(findings),
                    "model": model_by_qno.get(qno),
                }
            )
            continue
        if can_fix_findings_without_llm(findings):
            fixed = unescape_latex_entities(record, fields)
            after_findings = audit_record(fixed, fields)
            per_dir = out_dir / "per_question"
            write_json(per_dir / f"q{qno:02d}_fixed.json", fixed)
            write_json(per_dir / f"q{qno:02d}_findings_after.json", after_findings)
            fixed_by_qno[qno] = fixed
            results.append(
                {
                    "question_no": qno,
                    "status": "fixed" if not after_findings else "fixed_with_remaining_findings",
                    "before_finding_count": len(findings),
                    "after_finding_count": len(after_findings),
                    "before_html_entity_finding_count": html_entity_finding_count(findings),
                    "after_html_entity_finding_count": html_entity_finding_count(after_findings),
                    "changed_content_fingerprint": content_fingerprint(record, fields) != content_fingerprint(fixed, fields),
                    "model": "deterministic_html_unescape",
                    "structured_output": "none",
                }
            )
            continue

    def run_flagged_record(record: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
        qno = int(record["question_no"])
        findings = before_findings_by_qno.get(qno) or []
        per_dir = out_dir / "per_question"
        assigned_model = model_by_qno[qno]
        try:
            assert token is not None
            fixed, raw, elapsed = call_formatter(
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
            after_findings = audit_record(fixed, fields)
            before_fp = content_fingerprint(record, fields)
            after_fp = content_fingerprint(fixed, fields)
            changed_content = before_fp != after_fp
            write_text(per_dir / f"q{qno:02d}_raw_after.json.txt", raw)
            write_json(per_dir / f"q{qno:02d}_fixed.json", fixed)
            write_json(per_dir / f"q{qno:02d}_findings_after.json", after_findings)
            return qno, fixed, {
                "question_no": qno,
                "status": "fixed" if not after_findings else "fixed_with_remaining_findings",
                "elapsed_seconds": round(elapsed, 3),
                "before_finding_count": len(findings),
                "after_finding_count": len(after_findings),
                "before_html_entity_finding_count": html_entity_finding_count(findings),
                "after_html_entity_finding_count": html_entity_finding_count(after_findings),
                "changed_content_fingerprint": changed_content,
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

    llm_flagged = [record for record in flagged if int(record["question_no"]) not in fixed_by_qno]
    if not args.dry_run and llm_flagged:
        max_workers = args.max_workers or len(worker_models)
        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(llm_flagged)))) as pool:
            future_map = {pool.submit(run_flagged_record, record): int(record["question_no"]) for record in llm_flagged}
            for future in as_completed(future_map):
                qno, fixed, result = future.result()
                fixed_by_qno[qno] = fixed
                results.append(result)
    for record in records:
        qno = int(record["question_no"])
        if qno not in fixed_by_qno:
            fixed_by_qno[qno] = record
            if qno in flagged_by_qno and not args.dry_run:
                results.append(
                    {
                        "question_no": qno,
                        "status": "error",
                        "before_finding_count": len(before_findings_by_qno.get(qno) or []),
                        "error": "missing formatter result",
                        "model": model_by_qno.get(qno),
                    }
                )

    fixed_records = [fixed_by_qno[int(record["question_no"])] for record in records]
    after_findings_by_qno = {int(record["question_no"]): audit_record(record, fields) for record in fixed_records}
    merge_base_root = args.merge_base_qb_root or args.qb_root
    merge_run_dir = merge_base_root / args.run_id
    base_records = load_question_bank(merge_run_dir) if (merge_run_dir / "question_bank.json").exists() else fixed_records
    merged_records, applied_qnos, rejected_qnos = merge_fixed_fields(base_records, fixed_records, results, fields)
    summary = {
        "run_id": args.run_id,
        "source_stage": args.source_stage,
        "model": multi_model_name(worker_models),
        "worker_models": worker_models,
        "flagged_model_assignment": {str(qno): model_by_qno[qno] for qno in sorted(model_by_qno)},
        "enable_thinking": args.enable_thinking,
        "structured_output": args.structured_output,
        "dry_run": args.dry_run,
        "field_scope": sorted(fields),
        "question_count": len(records),
        "flagged_question_count": len(flagged),
        "before_finding_count": sum(len(items) for items in before_findings_by_qno.values()),
        "after_finding_count": sum(len(items) for items in after_findings_by_qno.values()),
        "merge_base_qb_root": str(merge_base_root),
        "applied_question_numbers": applied_qnos,
        "rejected_question_numbers": rejected_qnos,
        "write_back": bool(args.write_back and not args.dry_run),
        "results": sorted(results, key=lambda item: int(item.get("question_no") or 0)),
    }
    write_json(out_dir / "fixed_question_bank.json", fixed_records)
    write_json(out_dir / "merged_question_bank.json", merged_records)
    write_jsonl(out_dir / "merged_question_bank.jsonl", merged_records)
    write_json(out_dir / "audit_after.json", [item for qno in sorted(after_findings_by_qno) for item in after_findings_by_qno[qno]])
    write_json(out_dir / "summary.json", summary)
    if args.write_back and not args.dry_run:
        write_back_question_bank(merge_run_dir, merged_records, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
