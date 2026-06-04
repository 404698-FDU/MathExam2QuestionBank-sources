from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


SCHEMA_VERSION = 2
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BAILIAN_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_BAILIAN_MODEL = "qwen3.6-27b"
DEFAULT_V2_MATH_MODEL = "deepseek-v4-pro"
DEFAULT_V2_MATH_SCOPE = "all"
DEFAULT_V2_TIMEOUT = 180
BAILIAN_TOKEN_FILES = (
    ".bailian_token",
    ".dashscope_token",
    ".dashscope_api_key",
    ".aliyun_bailian_token",
)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY,
          run_dir TEXT NOT NULL,
          source_pdf TEXT,
          answer_pdf TEXT,
          extractor TEXT,
          validation_json TEXT NOT NULL,
          imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS questions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          question_no INTEGER NOT NULL,
          question_type TEXT NOT NULL,
          raw_text TEXT NOT NULL,
          normalized_text TEXT NOT NULL,
          edited_text TEXT,
          options_json TEXT NOT NULL,
          answer_raw TEXT,
          edited_answer TEXT,
          source_pages_json TEXT NOT NULL,
          source_bboxes_json TEXT NOT NULL,
          source_units_json TEXT NOT NULL,
          figures_json TEXT NOT NULL,
          confidence REAL NOT NULL,
          flags_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          reviewer_note TEXT NOT NULL DEFAULT '',
          auto_audit_json TEXT,
          updated_at TEXT NOT NULL,
          UNIQUE(run_id, question_no)
        );

        CREATE TABLE IF NOT EXISTS document_units (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          unit_id TEXT NOT NULL,
          page INTEGER NOT NULL,
          bbox_json TEXT NOT NULL,
          text TEXT NOT NULL,
          block_id TEXT NOT NULL,
          unit_order INTEGER NOT NULL,
          UNIQUE(run_id, unit_id)
        );

        CREATE TABLE IF NOT EXISTS figures (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          figure_id TEXT NOT NULL,
          page INTEGER NOT NULL,
          bbox_json TEXT NOT NULL,
          path TEXT NOT NULL,
          assigned_question_no INTEGER,
          UNIQUE(run_id, figure_id)
        );

        CREATE TABLE IF NOT EXISTS review_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    question_columns = table_columns(conn, "questions")
    for column, definition in {
        "answer_final": "TEXT",
        "answer_analysis": "TEXT",
        "answer_rubric": "TEXT",
    }.items():
        if column not in question_columns:
            conn.execute(f"ALTER TABLE questions ADD COLUMN {column} {definition}")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def question_audit_fingerprint(question: dict[str, Any]) -> str:
    payload = {
        "run_id": question.get("run_id"),
        "question_no": question.get("question_no"),
        "question_type": question.get("question_type"),
        "display_text": question.get("display_text"),
        "options": question.get("options"),
        "answer_final": question.get("answer_final"),
        "answer_analysis": question.get("answer_analysis"),
        "answer_rubric": question.get("answer_rubric"),
        "display_answer": question.get("display_answer"),
        "figures": [
            {
                "figure_id": figure.get("figure_id"),
                "page": figure.get("page"),
                "bbox": figure.get("bbox"),
            }
            for figure in question.get("figures_detail", []) or []
        ],
        "option_images": {
            label: {
                "figure_id": figure.get("figure_id"),
                "page": figure.get("page"),
                "bbox": figure.get("bbox"),
            }
            for label, figure in (question.get("option_images") or {}).items()
        },
        "answer_source": {
            "normalized_text": (question.get("answer_source_detail") or {}).get("normalized_text"),
            "raw_text": (question.get("answer_source_detail") or {}).get("raw_text"),
            "source_pages": (question.get("answer_source_detail") or {}).get("source_pages"),
        },
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def clean_final_answer(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^(?:答案为|答案|答|故答案为|所以答案为|得出|得到|可得|解得|出)\s*[:：]\s*", "", text)
    text = text.strip(" \t\r\n.;；,，。")
    if text.startswith("$") and text.endswith("$") and len(text) > 2:
        inner = text[1:-1].strip(" \t\r\n.;；,，。")
        return f"${inner}$" if inner else ""
    if text.endswith("$") and text.count("$") == 1:
        inner = text[:-1].strip(" \t\r\n.;；,，。")
        return inner
    return text.strip(" \t\r\n.;；,，。")


def reduce_equation_final(value: str) -> str:
    text = clean_final_answer(value)
    if text.startswith("$") and text.endswith("$") and len(text) > 2:
        inner = text[1:-1].strip()
        if "=" in inner:
            return clean_final_answer(inner.split("=")[-1])
        return text
    if "=" in text:
        return clean_final_answer(text.split("=")[-1])
    return text


def fill_blank_final_looks_like_prompt(value: str) -> bool:
    text = re.sub(r"\s+", "", value or "")
    bare = text.strip("$")
    if text == "$$" or not bare:
        return True
    if len(text) > 80:
        return True
    if (
        text.endswith(("为", "是", "则", "+", "-", "="))
        or bare.endswith("=")
        or "\\Rightarrow" in text
        or "\\Longrightarrow" in text
    ):
        return True
    prompt_markers = (
        "已知",
        "不等式",
        "函数",
        "直线",
        "数列",
        "展开式",
        "最小值",
        "最大值",
        "解集",
        "夹角",
        "系数",
        "问",
        "则",
    )
    return bool(len(text) > 20 and any(marker in text for marker in prompt_markers))


def fill_blank_final_looks_like_metadata(value: str) -> bool:
    text = re.sub(r"\s+", "", value or "")
    if not text:
        return False
    if re.search(r"[$\\=<>≤≥√π∞]", text):
        return False
    metadata_markers = (
        "函数与方程",
        "函数的周期性",
        "二次函数的性质",
        "知识点",
        "考点",
        "思想方法",
        "综合应用",
        "性质",
    )
    if any(marker in text for marker in metadata_markers):
        return True
    if any(marker in text for marker in ("更接近于", "面积更接近")):
        return True
    return "；" in text and any(marker in text for marker in ("函数", "方程", "几何", "概率", "统计", "性质"))


def invalid_fill_blank_final(value: str) -> bool:
    return (
        is_placeholder_answer(value)
        or fill_blank_final_looks_like_prompt(value)
        or fill_blank_final_looks_like_metadata(value)
    )


def is_placeholder_answer(value: str) -> bool:
    text = value.strip()
    return bool(
        "__" in text
        or "\\_" in text
        or "____" in text
        or re.search(r"[（(]\s*[）)]", text)
    )


def split_answer_markers(raw: str) -> tuple[str, str] | None:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return None
    saw_marker = False
    answer_lines: list[str] = []
    analysis_lines: list[str] = []
    mode = "answer"
    saw_answer_marker = False
    for line in lines:
        answer_match = re.match(r"^(?:\d{1,2}\s*[.．、,，]?\s*)?【答案】\s*(.*)$", line)
        if answer_match:
            saw_marker = True
            if not saw_answer_marker:
                answer_lines = []
            saw_answer_marker = True
            mode = "answer"
            line = answer_match.group(1).strip()
            if line:
                answer_lines.append(line)
            continue
        analysis_match = re.match(r"^【(?:思路分析|解析|解答|分析|归纳与总结|点评)】\s*(.*)$", line)
        if analysis_match:
            saw_marker = True
            mode = "analysis"
            line = analysis_match.group(1).strip().lstrip(":：").strip()
            if line:
                analysis_lines.append(line)
            continue
        if mode == "analysis":
            analysis_lines.append(line)
        else:
            answer_lines.append(line)
    if not saw_marker:
        return None
    return "\n".join(answer_lines).strip(), "\n".join(analysis_lines).strip()


def answer_raw_has_explicit_answer(answer_raw: str | None) -> bool:
    return bool(answer_raw and any(marker in answer_raw for marker in ("【答案】", "【解答】", "【答】")))


def extract_choice_final(text: str) -> str:
    compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if re.fullmatch(r"[ABCD]", compact):
        return compact
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if re.fullmatch(r"[ABCD]", first_line):
        return first_line
    inline_answer = re.search(r"(?:\[答\]|【答】)\s*[（(]\s*([ABCD])\s*[）)]", compact)
    if inline_answer:
        return inline_answer.group(1)
    first_answer = re.match(r"^\s*([ABCD])\s*[，,、.．:：]", first_line)
    if first_answer:
        return first_answer.group(1)
    patterns = (
        r"(?:故|所以|因此|答案|正确答案|应选|故选|选|选择|答案为|选项)\s*[:：]?\s*([ABCD])\b",
        r"\b([ABCD])\s*(?:正确|符合题意|为答案)",
    )
    for pattern in patterns:
        matches = re.findall(pattern, compact)
        if matches:
            return matches[-1]
    return ""


def normalize_choice_match_text(value: str) -> str:
    text = value.lower()
    text = text.replace("（", "(").replace("）", ")").replace("，", ",")
    text = re.sub(r"\\[a-zA-Z]+", "", text)
    text = re.sub(r"[$\s`~!@#%^&*_+=|\\:;\"'<>,.?/，。；：、“”‘’（）()\[\]{}-]", "", text)
    return text


def infer_choice_final_from_options(options: dict[str, str], analysis_text: str | None) -> str:
    if not options or not analysis_text:
        return ""
    text = analysis_text
    for marker in ("【解析】", "解析：", "解析:"):
        if marker in text:
            text = text.split(marker)[-1]
    text = text[-500:]
    normalized_text = normalize_choice_match_text(text)
    matches: list[str] = []
    for label, option in sorted(options.items()):
        normalized_option = normalize_choice_match_text(option)
        if len(normalized_option) >= 3 and normalized_option in normalized_text:
            matches.append(label)
    if len(matches) == 1:
        return matches[0]
    value_candidates = re.findall(
        r"(?:最大值|最小值|取值|结果|答案|等于|为|是)\s*[:：]?\s*([^，。；;\n]{1,40})",
        text,
    )
    for candidate in reversed(value_candidates):
        candidate = re.sub(r"^(?:为|是|等于)\s*", "", candidate.strip())
        normalized_candidate = normalize_choice_match_text(candidate)
        if not normalized_candidate:
            continue
        exact_matches = [
            label
            for label, option in sorted(options.items())
            if normalize_choice_match_text(option) == normalized_candidate
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
    if any(marker in text for marker in ("只有一个", "唯一", "恰有一个")):
        one_matches = [
            label
            for label, option in sorted(options.items())
            if normalize_choice_match_text(option) in {"1", "一个", "一"}
        ]
        if len(one_matches) == 1:
            return one_matches[0]
    if "钝角" in text and "锐角" not in text and "直角" not in text:
        obtuse_matches = [label for label, option in options.items() if "钝角" in option and "可能" not in option]
        if len(obtuse_matches) == 1:
            return obtuse_matches[0]
    return ""


def extract_fill_blank_final_from_analysis(text: str) -> str:
    cleaned = re.sub(r"【(?:解析|点评|分析|说明)】[:：]?", "\n", text)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    for line in reversed(lines):
        tail_matches = re.findall(r"=\s*([^=，。；;\n$]{1,40})\$?\s*$", line)
        if tail_matches:
            candidate = clean_final_answer(tail_matches[-1])
            if candidate and not is_placeholder_answer(candidate):
                return candidate
        if len(line) > 180:
            continue
        if any(marker in line for marker in ("归纳", "总结", "本题考查", "基础题", "中档题", "难题")):
            continue
        if "考查" in line and not re.search(r"[$=0-9０-９]", line):
            continue
        math_matches = re.findall(r"\$([^$]{1,80})\$", line)
        if math_matches and any(marker in line for marker in ("答案", "所以", "故", "即", "得", "为", "是", "=", "∴")):
            candidate = math_matches[-1].strip()
            if "\\Rightarrow" in candidate or "\\Longrightarrow" in candidate:
                continue
            if "=" in candidate:
                candidate = reduce_equation_final(candidate)
            if not candidate:
                continue
            final = clean_final_answer("$" + candidate + "$")
            if final and not invalid_fill_blank_final(final):
                return final
        for pattern in (
            r"(?:答案(?:为)?|故|所以|因此|即|∴|得出|得到|可得|解得)\s*[:：]?\s*([^，。；;\n]{1,80})",
            r"(?:为|是|得)\s*([^，。；;\n]{1,80})$",
            r"=\s*([^=，。；;\n]{1,80})$",
        ):
            matches = re.findall(pattern, line)
            if matches:
                candidate = clean_final_answer(matches[-1])
                if candidate.count("=") >= 2 or candidate.startswith("="):
                    candidate = clean_final_answer(candidate.split("=")[-1])
                if candidate and not is_placeholder_answer(candidate):
                    return candidate
    return ""


def extract_fill_blank_final_from_answer_text(text: str) -> str:
    compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if not compact:
        return ""

    short_direct = clean_final_answer(compact)
    if (
        short_direct
        and (len(short_direct) <= 40 or (short_direct.startswith("$") and short_direct.endswith("$") and len(short_direct) <= 120))
        and not is_placeholder_answer(short_direct)
        and not fill_blank_final_looks_like_prompt(short_direct)
    ):
        return short_direct

    underline_matches = re.findall(r"\\underline\{(?:\\quad\s*)?([^{}]{1,120})\}", compact)
    for candidate in reversed(underline_matches):
        final = reduce_equation_final(candidate)
        if final and not is_placeholder_answer(final):
            return final

    blank_compact = re.sub(r"\\_", "§", compact)
    count_matches = re.findall(r"共有\s*([^，。；;\n]{1,40}?)\s*种", compact)
    for candidate in reversed(count_matches):
        final = reduce_equation_final(candidate)
        if final and len(final) <= 40 and not is_placeholder_answer(final):
            return final
    explicit_patterns = (
        r"(?:故答案为|所以答案为|答案为|结果为)\s*[:：]?\s*([^，。；;\n]{1,120})",
        r"(?:解集为|夹角为|最小值为|最大值为|系数和为|则)\s*([^，。；;\n]{1,120})$",
    )
    for pattern in explicit_patterns:
        matches = re.findall(pattern, compact)
        for candidate in reversed(matches):
            final = reduce_equation_final(candidate)
            if final and len(final) <= 80 and not is_placeholder_answer(final):
                return final
    math_matches = re.findall(r"\$([^$]{1,120})\$", compact)
    if math_matches and any(marker in compact for marker in ("则", "为", "是", "答案", "解集", "夹角", "最小值", "最大值")):
        final = reduce_equation_final("$" + math_matches[-1] + "$")
        if final and len(final) <= 80 and not is_placeholder_answer(final):
            return final
    patterns = (
        r"(?:§+|_{2,})\s*([^§_，。；;\n]{1,120}?)(?:§+|_{2,}|。|$)",
        r"—+\s*([^—，。；;\n]{1,120}?)\s*—+",
        r"=\s*([^=§，。；;\n_]{1,60}?)(?:§+|_{2,}|。|$)",
        r"(?:是|为|得)\s*([^§_，。；;\n]{1,80}?)(?:§+|_{2,}|。|$)",
    )
    for pattern in patterns:
        matches = re.findall(pattern, blank_compact)
        for candidate in reversed(matches):
            final = reduce_equation_final(candidate.strip(" _"))
            if final.count("$") == 1 and final.endswith("$"):
                final = reduce_equation_final(final[:-1])
            if (
                final
                and len(final) <= 80
                and not is_placeholder_answer(final)
                and "解析" not in final
                and "考查" not in final
            ):
                return final
    if any(marker in compact for marker in ("应填入", "填入", "答案为", "结果为")):
        math_matches = re.findall(r"\$([^$]{1,120})\$", compact)
        for candidate in reversed(math_matches):
            final = reduce_equation_final("$" + candidate + "$")
            if final and len(final) <= 80 and not is_placeholder_answer(final):
                return final
    return ""


def split_answer_fields(question_type: str, answer_raw: str | None) -> dict[str, str]:
    raw = (answer_raw or "").strip()
    if not raw:
        return {"answer_final": "", "answer_analysis": "", "answer_rubric": ""}

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    compact = " ".join(lines)
    analysis_markers = (
        "解：",
        "解:",
        "证明",
        "因为",
        "由于",
        "所以",
        "故",
        "得",
        "由",
        "当且仅当",
    )
    rubric_markers = ("评分", "给分", "得分", "满分", "分)")

    rubric_lines = [line for line in lines if any(marker in line for marker in rubric_markers)]
    non_rubric_lines = [line for line in lines if line not in rubric_lines]
    marked = split_answer_markers(raw)
    if marked:
        marked_answer, marked_analysis = marked
        if question_type in {"single_choice", "choice"}:
            final = extract_choice_final(marked_answer) or extract_choice_final(marked_analysis)
            return {"answer_final": final, "answer_analysis": marked_analysis or marked_answer, "answer_rubric": "\n".join(rubric_lines)}
        if question_type == "fill_blank":
            answer_final = extract_fill_blank_final_from_answer_text(marked_answer)
            analysis_final = extract_fill_blank_final_from_analysis(marked_analysis or raw)
            final = answer_final
            if not final or invalid_fill_blank_final(final):
                final = analysis_final
            if not final:
                final = clean_final_answer(marked_answer)
            if invalid_fill_blank_final(final):
                final = ""
            return {"answer_final": final, "answer_analysis": marked_analysis, "answer_rubric": "\n".join(rubric_lines)}
        answer_analysis = "\n".join(part for part in (marked_answer, marked_analysis) if part)
        return {"answer_final": "", "answer_analysis": answer_analysis, "answer_rubric": "\n".join(rubric_lines)}

    if question_type in {"single_choice", "choice"} or re.fullmatch(r"[ABCD]", compact):
        final = extract_choice_final(raw)
        analysis = "\n".join(line for line in non_rubric_lines if line != final)
        return {"answer_final": final, "answer_analysis": analysis, "answer_rubric": "\n".join(rubric_lines)}

    if question_type == "fill_blank":
        analysis_marker = re.search(r"(?:【(?:解析|解答|分析|思路分析)】|解析[:：]|解答[:：]|分析[:：])", raw)
        if analysis_marker:
            answer_part = raw[: analysis_marker.start()].strip()
            final_from_answer = extract_fill_blank_final_from_answer_text(answer_part)
            final = final_from_answer or extract_fill_blank_final_from_analysis(raw)
            return {
                "answer_final": final if final and not invalid_fill_blank_final(final) else "",
                "answer_analysis": raw,
                "answer_rubric": "\n".join(rubric_lines),
            }
        if any(marker in raw for marker in ("【解析】", "【解答】", "【分析】")):
            final_from_analysis = extract_fill_blank_final_from_analysis(raw)
            if final_from_analysis and not invalid_fill_blank_final(final_from_analysis):
                return {
                    "answer_final": final_from_analysis,
                    "answer_analysis": raw,
                    "answer_rubric": "\n".join(rubric_lines),
                }
        if len(non_rubric_lines) == 1:
            line = non_rubric_lines[0]
            if len(line) <= 30 and not any(marker in line for marker in analysis_markers):
                final = clean_final_answer(line)
                return {
                    "answer_final": "" if invalid_fill_blank_final(final) else final,
                    "answer_analysis": line if invalid_fill_blank_final(final) else "",
                    "answer_rubric": "\n".join(rubric_lines),
                }
            final = extract_fill_blank_final_from_answer_text(line)
            if not final:
                tail = re.split(r"(?:故|所以|答案为|结果为|等于|=)", line)[-1].strip(" 。.;；")
                final = reduce_equation_final(tail if 0 < len(tail) <= 40 else line)
            if invalid_fill_blank_final(final):
                return {"answer_final": "", "answer_analysis": line, "answer_rubric": "\n".join(rubric_lines)}
            analysis = "" if final == line else line
            return {"answer_final": final, "answer_analysis": analysis, "answer_rubric": "\n".join(rubric_lines)}
        final = clean_final_answer(non_rubric_lines[-1] if non_rubric_lines else compact)
        if invalid_fill_blank_final(final):
            final = ""
        analysis = "\n".join(non_rubric_lines[:-1])
        return {"answer_final": final, "answer_analysis": analysis, "answer_rubric": "\n".join(rubric_lines)}

    return {
        "answer_final": "",
        "answer_analysis": "\n".join(non_rubric_lines) or raw,
        "answer_rubric": "\n".join(rubric_lines),
    }


def looks_like_choice_answer(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"[ABCD]", value.strip()))


def question_text_has_fill_blank(value: str | None) -> bool:
    text = value or ""
    return "__" in text or "\\_" in text or "____" in text


def compact_for_overlap(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "")


def answer_raw_looks_like_stem(question_text: str | None, answer_raw: str | None) -> bool:
    stem = compact_for_overlap(question_text)
    answer = compact_for_overlap(answer_raw)
    if len(stem) < 20 or len(answer) < 20:
        return False
    prefix_len = min(60, len(stem), len(answer))
    if stem[:prefix_len] == answer[:prefix_len]:
        return True
    short, long = (stem, answer) if len(stem) <= len(answer) else (answer, stem)
    return len(short) >= 40 and short in long


def _salvage_truncated_json(text: str) -> str:
    """Try to repair JSON truncated mid-string or mid-structure."""
    repaired = text.rstrip()
    # Check if we're inside an unterminated string
    in_str = False
    escaped = False
    for ch in repaired:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_str = not in_str
    if in_str:
        repaired += '"'
    # Close any open brackets in reverse opening order
    # Track the sequence of opens so we close correctly: [ → ] , { → }
    open_stack: list[str] = []
    in_str = False
    escaped = False
    for ch in repaired:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            open_stack.append(ch)
        elif ch in "}]":
            # Pop matching opener if stack top matches
            if open_stack:
                expected = "{" if ch == "}" else "["
                if open_stack[-1] == expected:
                    open_stack.pop()
    for opener in reversed(open_stack):
        repaired += "}" if opener == "{" else "]"
    return repaired


def json_loads_lenient(value: str) -> Any:
    escaped = re.sub(r'(?<!\\)\\(?!["\\/]|u[0-9a-fA-F]{4})', r"\\\\", value)
    if escaped != value:
        try:
            return json.loads(escaped)
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        msg = str(exc)
        # Known truncation patterns — try to salvage
        if "Unterminated string" in msg or "Unterminated array" in msg or "Expecting" in msg:
            salvaged = _salvage_truncated_json(value)
            try:
                return json.loads(salvaged)
            except json.JSONDecodeError:
                pass
        if "Invalid \\escape" in msg:
            return json.loads(escaped)
        raise


def infer_question_type(q: dict[str, Any], figures_by_question: dict[int, list[dict[str, Any]]]) -> str:
    qtype = str(q.get("question_type") or "unknown")
    question_no = int(q.get("question_no", 0))
    text = str(q.get("normalized_text") or q.get("raw_text") or "")
    answer_raw = q.get("answer_raw")
    if (
        qtype in {"single_choice", "choice"}
        and not q.get("options")
        and not looks_like_choice_answer(answer_raw)
        and (question_text_has_fill_blank(text) or question_no <= 12)
    ):
        return "fill_blank"
    if qtype != "unknown":
        return qtype
    figure_count = len(figures_by_question.get(question_no, [])) or len(q.get("figures", []) or [])
    has_choice_blank = bool(re.search(r"[（(]\s*[）)]", text))
    if 13 <= question_no <= 16 and looks_like_choice_answer(answer_raw):
        return "single_choice"
    if looks_like_choice_answer(answer_raw) and (figure_count >= 4 or has_choice_blank):
        return "single_choice"
    return qtype


def normalize_question_flags(q: dict[str, Any], question_type: str) -> list[str]:
    flags = [str(flag) for flag in (q.get("flags") or []) if str(flag)]
    if question_type not in {"single_choice", "choice"}:
        flags = [flag for flag in flags if flag != "choice_options_incomplete"]
    return flags


def derive_option_image_map(question: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if question.get("options"):
        return {}
    figures = question.get("figures_detail") or []
    if question.get("question_type") != "single_choice" or len(figures) < 4:
        return {}
    ordered = sorted(
        figures[:4],
        key=lambda fig: (
            int(fig.get("page") or 0),
            round(float((fig.get("bbox") or [0, 0, 0, 0])[1]) / 25.0),
            float((fig.get("bbox") or [0, 0, 0, 0])[0]),
        ),
    )
    return {label: ordered[index] for index, label in enumerate("ABCD") if index < len(ordered)}


def load_answer_source(run_dir: Path, question_no: int) -> dict[str, Any] | None:
    path = run_dir / "answers_raw.json"
    if not path.exists():
        return None
    try:
        answers = read_json(path)
    except (json.JSONDecodeError, OSError):
        return None
    for item in answers:
        if int(item.get("question_no", -1)) == int(question_no):
            return item
    return None


def load_run_paths(run_dir: Path) -> dict[str, Path]:
    required = {
        "questions": run_dir / "questions_normalized.json",
        "figures": run_dir / "figures_assigned.json",
        "validation": run_dir / "validation_report.json",
        "selected_pair": run_dir / "selected_pair.json",
        "paper_blocks": run_dir / "paper" / "ocr_blocks.json",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        raise SystemExit(f"Missing run artifacts: {', '.join(missing)} in {run_dir}")
    return required


def import_run(db_path: Path, run_dir: Path, run_id: str | None = None, replace: bool = True) -> str:
    run_dir = run_dir.resolve()
    paths = load_run_paths(run_dir)
    inferred_run_id = run_id or run_dir.name
    questions = read_json(paths["questions"])
    figures = read_json(paths["figures"])
    validation = read_json(paths["validation"])
    selected_pair = read_json(paths["selected_pair"])
    paper_blocks = read_json(paths["paper_blocks"])
    figures_by_question: dict[int, list[dict[str, Any]]] = {}
    for fig in figures:
        assigned = fig.get("assigned_question_no")
        if assigned is not None:
            figures_by_question.setdefault(int(assigned), []).append(fig)

    conn = connect_db(db_path)
    init_db(conn)
    preserved_audits: dict[int, dict[str, str]] = {}
    if replace:
        existing_rows = conn.execute(
            "SELECT id, question_no, auto_audit_json FROM questions WHERE run_id = ?",
            (inferred_run_id,),
        ).fetchall()
        for existing in existing_rows:
            if not existing["auto_audit_json"]:
                continue
            detail = get_question_detail(conn, int(existing["id"]))
            if not detail:
                continue
            preserved_audits[int(existing["question_no"])] = {
                "fingerprint": question_audit_fingerprint(detail),
                "auto_audit_json": existing["auto_audit_json"],
            }
    with conn:
        if replace:
            conn.execute("DELETE FROM runs WHERE run_id = ?", (inferred_run_id,))
        conn.execute(
            """
            INSERT INTO runs(run_id, run_dir, source_pdf, answer_pdf, extractor, validation_json, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inferred_run_id,
                str(run_dir),
                selected_pair.get("blank"),
                selected_pair.get("answer_like"),
                paper_blocks.get("extractor", "local"),
                json_text(validation),
                utc_now(),
            ),
        )
        for unit in paper_blocks.get("text_units", []):
            conn.execute(
                """
                INSERT INTO document_units(run_id, unit_id, page, bbox_json, text, block_id, unit_order)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inferred_run_id,
                    unit["unit_id"],
                    int(unit["page"]),
                    json_text(unit["bbox"]),
                    unit["text"],
                    unit["block_id"],
                    int(unit.get("order", 0)),
                ),
            )
        for fig in figures:
            conn.execute(
                """
                INSERT INTO figures(run_id, figure_id, page, bbox_json, path, assigned_question_no)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    inferred_run_id,
                    fig["figure_id"],
                    int(fig["page"]),
                    json_text(fig["bbox"]),
                    fig.get("path", ""),
                    fig.get("assigned_question_no"),
                ),
            )
        now = utc_now()
        for q in questions:
            question_type = infer_question_type(q, figures_by_question)
            flags = normalize_question_flags(q, question_type)
            status = "needs_review" if flags else "pending"
            answer_parts = split_answer_fields(question_type, q.get("answer_raw"))
            if question_type in {"single_choice", "choice"} and not answer_parts["answer_final"]:
                inferred_choice = infer_choice_final_from_options(q.get("options", {}), answer_parts["answer_analysis"] or q.get("answer_raw"))
                if inferred_choice:
                    answer_parts["answer_final"] = inferred_choice
            if (
                question_type in {"fill_blank", "single_choice", "choice"}
                and not answer_parts["answer_final"]
                and not answer_raw_has_explicit_answer(q.get("answer_raw"))
                and answer_raw_looks_like_stem(
                q.get("normalized_text") or q.get("raw_text"),
                q.get("answer_raw"),
                )
            ):
                answer_parts["answer_final"] = ""
                answer_parts["answer_analysis"] = q.get("answer_raw") or ""
            cursor = conn.execute(
                """
                INSERT INTO questions(
                  run_id, question_no, question_type, raw_text, normalized_text, edited_text,
                  options_json, answer_raw, edited_answer, source_pages_json, source_bboxes_json,
                  source_units_json, figures_json, confidence, flags_json, status, reviewer_note,
                  auto_audit_json, updated_at, answer_final, answer_analysis, answer_rubric
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inferred_run_id,
                    int(q["question_no"]),
                    question_type,
                    q["raw_text"],
                    q["normalized_text"],
                    None,
                    json_text(q.get("options", {})),
                    q.get("answer_raw"),
                    None,
                    json_text(q.get("source_pages", [])),
                    json_text(q.get("source_bboxes", {})),
                    json_text(q.get("source_units", [])),
                    json_text(q.get("figures", [])),
                    float(q.get("confidence", 0.0)),
                    json_text(flags),
                    status,
                    "",
                    None,
                    now,
                    answer_parts["answer_final"],
                    answer_parts["answer_analysis"],
                    answer_parts["answer_rubric"],
                ),
            )
            preserved = preserved_audits.get(int(q["question_no"]))
            if preserved and preserved.get("auto_audit_json"):
                detail = get_question_detail(conn, int(cursor.lastrowid))
                if detail and question_audit_fingerprint(detail) == preserved.get("fingerprint"):
                    conn.execute(
                        "UPDATE questions SET auto_audit_json = ? WHERE id = ?",
                        (preserved["auto_audit_json"], int(cursor.lastrowid)),
                    )
    conn.close()
    return inferred_run_id


def row_to_question(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "question_no": row["question_no"],
        "question_type": row["question_type"],
        "raw_text": row["raw_text"],
        "normalized_text": row["normalized_text"],
        "edited_text": row["edited_text"],
        "display_text": row["edited_text"] or row["normalized_text"],
        "options": json.loads(row["options_json"]),
        "answer_raw": row["answer_raw"],
        "answer_final": row["answer_final"] or "",
        "answer_analysis": row["answer_analysis"] or "",
        "answer_rubric": row["answer_rubric"] or "",
        "edited_answer": row["edited_answer"],
        "display_answer": row["edited_answer"] or row["answer_raw"] or "",
        "source_pages": json.loads(row["source_pages_json"]),
        "source_bboxes": json.loads(row["source_bboxes_json"]),
        "source_units": json.loads(row["source_units_json"]),
        "figures": json.loads(row["figures_json"]),
        "confidence": row["confidence"],
        "flags": json.loads(row["flags_json"]),
        "status": row["status"],
        "reviewer_note": row["reviewer_note"],
        "auto_audit": json.loads(row["auto_audit_json"]) if row["auto_audit_json"] else None,
        "updated_at": row["updated_at"],
    }


def rule_audit_question(question: dict[str, Any]) -> dict[str, Any]:
    text = question["display_text"]
    answer = question["display_answer"]
    issues: list[dict[str, str]] = []
    checks: list[str] = []

    def add_issue(code: str, message: str, severity: str = "warning") -> None:
        issues.append({"code": code, "message": message, "severity": severity})

    if len(text.strip()) < 8:
        add_issue("short_stem", "题干过短，可能漏切或 OCR 缺失。", "error")
    else:
        checks.append("stem_length_ok")

    if question["question_no"] <= 16:
        if not answer.strip():
            add_issue("missing_answer", "客观题/填空题没有匹配到答案。", "error")
        else:
            checks.append("answer_present")

    if question["question_type"] == "single_choice":
        labels = set(question["options"].keys())
        missing = [label for label in ("A", "B", "C", "D") if label not in labels]
        if missing:
            add_issue("choices_incomplete", "选择题选项不完整：" + ",".join(missing), "error")
        else:
            checks.append("choices_complete")

    if question["question_type"] == "unknown":
        add_issue("unknown_type", "题型未识别，需要人工确认。")

    dollar_count = text.count("$")
    if dollar_count % 2:
        add_issue("latex_unbalanced_dollar", "LaTeX 内联公式美元符号数量不平衡。", "error")
    else:
        checks.append("latex_dollar_balanced")

    if any("\ue000" <= ch <= "\uf8ff" for ch in text):
        add_issue("private_unicode", "题干仍包含私有区数学字形。", "error")
    else:
        checks.append("no_private_unicode")

    if re.search(r"https?://|REPLiX|NO COMMERC", text, re.IGNORECASE):
        add_issue("watermark_leaked", "题干疑似包含页眉、水印或来源链接。", "error")

    if question["figures"]:
        checks.append("has_figure_candidates")

    verdict = "pass"
    if any(issue["severity"] == "error" for issue in issues):
        verdict = "fail"
    elif issues:
        verdict = "review"

    return {
        "provider": "rule_audit",
        "verdict": verdict,
        "issues": issues,
        "checks": checks,
        "summary": "本地自动审核通过。" if verdict == "pass" else "需要人工复核：" + "；".join(issue["message"] for issue in issues),
        "created_at": utc_now(),
    }


def load_bailian_token() -> str | None:
    for env_name in ("DASHSCOPE_API_KEY", "BAILIAN_API_KEY", "ALIYUN_BAILIAN_API_KEY", "QWEN_API_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value.removeprefix("Bearer ").strip()
    for file_name in BAILIAN_TOKEN_FILES:
        path = WORKSPACE_ROOT / file_name
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value.removeprefix("Bearer ").strip()
    env_path = WORKSPACE_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() in {"DASHSCOPE_API_KEY", "BAILIAN_API_KEY", "ALIYUN_BAILIAN_API_KEY", "QWEN_API_KEY"}:
                value = value.strip().strip('"').strip("'")
                if value:
                    return value.removeprefix("Bearer ").strip()
    return None


def _first_json_cut(text: str) -> str:
    """Return the slice from the first '{' through its matching '}' (balanced)."""
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    # unbalanced — fall back to rfind
    end = text.rfind("}")
    if end > start:
        return text[start : end + 1]
    return text


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1).strip()
    try:
        return json_loads_lenient(stripped)
    except json.JSONDecodeError:
        cut = _first_json_cut(stripped)
        if cut != stripped:
            return json_loads_lenient(cut)
        raise


def normalize_llm_audit_payload(payload: dict[str, Any], model: str, rule_audit: dict[str, Any]) -> dict[str, Any]:
    verdict = str(payload.get("verdict", "review")).lower()
    if verdict not in {"pass", "review", "fail"}:
        verdict = "review"
    issues = payload.get("issues", [])
    if not isinstance(issues, list):
        issues = [{"code": "invalid_issues", "message": str(issues), "severity": "warning"}]
    normalized_issues = []
    for item in issues:
        if isinstance(item, dict):
            normalized_issues.append(
                {
                    "code": str(item.get("code", "llm_issue")).strip().lower() or "llm_issue",
                    "message": str(item.get("message", "")),
                    "severity": str(item.get("severity", "warning")).strip().lower() or "warning",
                    "suggested_fix": str(item.get("suggested_fix", "")),
                }
            )
        else:
            normalized_issues.append({"code": "llm_issue", "message": str(item), "severity": "warning", "suggested_fix": ""})
    checks = payload.get("checks", [])
    if not isinstance(checks, list):
        checks = [str(checks)]
    return {
        "provider": "aliyun_bailian",
        "model": model,
        "verdict": verdict,
        "issues": normalized_issues,
        "checks": [str(check) for check in checks],
        "summary": str(payload.get("summary", "")) or ("百炼 Qwen 审核通过。" if verdict == "pass" else "百炼 Qwen 建议复核。"),
        "confidence": payload.get("confidence", None),
        "rule_preaudit": rule_audit,
        "created_at": utc_now(),
    }


def call_bailian_chat(messages: list[dict[str, str]], model: str, timeout: int = 90) -> str:
    token = load_bailian_token()
    if not token:
        raise RuntimeError("未找到百炼 API token。请设置 DASHSCOPE_API_KEY/BAILIAN_API_KEY，或在项目根目录放置 .bailian_token。")
    endpoint = os.environ.get("BAILIAN_ENDPOINT", DEFAULT_BAILIAN_ENDPOINT).strip() or DEFAULT_BAILIAN_ENDPOINT
    body_payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "top_p": 0.8,
        "response_format": {"type": "json_object"},
    }
    if "qwen3-vl-8b" not in str(model).lower():
        body_payload["enable_thinking"] = False
    body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"百炼 API HTTP {exc.code}: {error_body[:600]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"百炼 API 网络错误: {exc}") from exc
    try:
        return response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"百炼 API 返回格式异常: {json.dumps(response_payload, ensure_ascii=False)[:600]}") from exc


def build_bailian_audit_messages(question: dict[str, Any], rule_audit: dict[str, Any]) -> list[dict[str, str]]:
    question_payload = {
        "question_no": question["question_no"],
        "question_type": question["question_type"],
        "stem_latex": question["display_text"],
        "options": question["options"],
        "answer": question["display_answer"],
        "source_pages": question.get("source_pages", []),
        "has_figures": bool(question.get("figures")),
        "figure_count": len(question.get("figures", [])),
        "rule_preaudit": rule_audit,
    }
    system = (
        "你是数学试卷 OCR 入库的自动审核员。你的任务是审核结构化题目是否适合进入题库。"
        "重点检查：题干是否完整、LaTeX 是否明显错误、答案是否匹配、选择题选项是否完整、"
        "是否残留 OCR 噪声/水印、是否需要人工确认配图或题型。"
        "你不能凭空改题，只能给审核结论和修改建议。"
    )
    user = (
        "/no_think\n请审核下面题库候选题，并只返回一个 JSON 对象，不要 Markdown，不要解释性前后缀。\n"
        "JSON schema:\n"
        "{\n"
        '  "verdict": "pass|review|fail",\n'
        '  "issues": [{"code": "string", "severity": "warning|error", "message": "中文问题说明", "suggested_fix": "中文修改建议"}],\n'
        '  "checks": ["已通过的检查项"],\n'
        '  "summary": "一句中文总结",\n'
        '  "confidence": 0.0\n'
        "}\n"
        "判定标准：\n"
        "- pass：题干、答案、选项/题型基本可靠，可进入人工快速确认。\n"
        "- review：存在图形题型、题型不确定、答案可能不完整、LaTeX 可疑等，需要人工复核。\n"
        "- fail：明显漏题、乱码、水印进入题干、答案缺失或结构严重错误。\n\n"
        "题目数据：\n"
        + json.dumps(question_payload, ensure_ascii=False, indent=2)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def qwen_audit_question(question: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    selected_model = model or os.environ.get("BAILIAN_MODEL", DEFAULT_BAILIAN_MODEL).strip() or DEFAULT_BAILIAN_MODEL
    rule_audit = rule_audit_question(question)
    messages = build_bailian_audit_messages(question, rule_audit)
    try:
        content = call_bailian_chat(messages, selected_model)
        payload = extract_json_object(content)
        return normalize_llm_audit_payload(payload, selected_model, rule_audit)
    except Exception as exc:
        fallback = dict(rule_audit)
        fallback["provider"] = "aliyun_bailian"
        fallback["model"] = selected_model
        fallback["verdict"] = "review" if fallback.get("verdict") == "pass" else fallback.get("verdict", "review")
        fallback["issues"] = list(fallback.get("issues", [])) + [
            {
                "code": "bailian_api_error",
                "message": f"百炼 Qwen 审核调用失败：{exc}",
                "severity": "warning",
                "suggested_fix": "检查 .bailian_token、模型名、网络或百炼服务状态后重试。",
            }
        ]
        fallback["summary"] = "百炼 Qwen 审核调用失败，已保留本地规则预检结果。"
        fallback["rule_preaudit"] = rule_audit
        fallback["created_at"] = utc_now()
        return fallback


def audit_question(question: dict[str, Any], provider: str | None = None, model: str | None = None) -> dict[str, Any]:
    selected_provider = (provider or os.environ.get("AUDIT_PROVIDER", "qwen")).strip().lower()
    if selected_provider in {"rule", "local", "stub"}:
        return rule_audit_question(question)
    return qwen_audit_question(question, model=model)


def run_audit(
    db_path: Path,
    run_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    question_id: int | None = None,
    question_no: int | None = None,
) -> dict[str, Any]:
    conn = connect_db(db_path)
    init_db(conn)
    filters: list[str] = []
    params: list[Any] = []
    if run_id:
        filters.append("run_id = ?")
        params.append(run_id)
    if question_id is not None:
        filters.append("id = ?")
        params.append(question_id)
    if question_no is not None:
        filters.append("question_no = ?")
        params.append(question_no)
    where = "WHERE " + " AND ".join(filters) if filters else ""
    rows = conn.execute(f"SELECT * FROM questions {where} ORDER BY run_id, question_no", tuple(params)).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        question = row_to_question(row)
        audit = audit_question(question, provider=provider, model=model)
        counts[audit["verdict"]] = counts.get(audit["verdict"], 0) + 1
        with conn:
            conn.execute(
                "UPDATE questions SET auto_audit_json = ?, updated_at = ? WHERE id = ?",
                (json_text(audit), utc_now(), row["id"]),
            )
            conn.execute(
                "INSERT INTO review_events(question_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], "auto_audit", json_text(audit), utc_now()),
            )
    conn.close()
    return {"audited": len(rows), "counts": counts}


def run_pipeline_v2_single_audit(
    db_path: Path,
    run_id: str,
    question_no: int,
    vision_model: str | None = None,
    math_model: str | None = None,
    math_scope: str = DEFAULT_V2_MATH_SCOPE,
    timeout: int = DEFAULT_V2_TIMEOUT,
) -> dict[str, Any]:
    import pipeline_v2

    args = argparse.Namespace(
        db=str(db_path),
        run_id=run_id,
        run_dir=None,
        no_replace=True,
        question_no=question_no,
        limit=None,
        reuse_probes=True,
        reuse_db_stages=True,
        vision_model=vision_model or DEFAULT_BAILIAN_MODEL,
        math_model=math_model or DEFAULT_V2_MATH_MODEL,
        math_scope=math_scope,
        timeout=timeout,
        vision_out_dir=str(pipeline_v2.DEFAULT_VISION_DIR),
        math_out_dir=str(pipeline_v2.DEFAULT_MATH_DIR),
    )
    summary = pipeline_v2.audit_run(args)
    conn = connect_db(db_path)
    row = conn.execute(
        "SELECT auto_audit_json FROM questions WHERE run_id = ? AND question_no = ?",
        (run_id, question_no),
    ).fetchone()
    conn.close()
    if not row or not row["auto_audit_json"]:
        raise RuntimeError(f"Pipeline v2 audit did not write a result for {run_id} Q{question_no}.")
    return {"audit": json.loads(row["auto_audit_json"]), "summary": summary}


def run_pipeline_v2_batch_audit(
    db_path: Path,
    run_id: str | None,
    vision_model: str | None = None,
    math_model: str | None = None,
    math_scope: str = DEFAULT_V2_MATH_SCOPE,
    timeout: int = DEFAULT_V2_TIMEOUT,
    max_questions: int | None = None,
) -> dict[str, Any]:
    import batch_pipeline_v2_runner
    import pipeline_v2

    args = argparse.Namespace(
        db=str(db_path),
        run_id=[run_id] if run_id else None,
        run_prefix="mineru_vlm_batch_",
        mineru_batch_only=True,
        limit_runs=None,
        limit_questions_per_run=None,
        max_questions=max_questions,
        only_missing=True,
        dry_run=False,
        reuse_probes=True,
        reuse_db_stages=True,
        vision_model=vision_model or DEFAULT_BAILIAN_MODEL,
        math_model=math_model or DEFAULT_V2_MATH_MODEL,
        math_scope=math_scope,
        timeout=timeout,
        vision_out_dir=str(pipeline_v2.DEFAULT_VISION_DIR),
        math_out_dir=str(pipeline_v2.DEFAULT_MATH_DIR),
    )
    return batch_pipeline_v2_runner.run_batch(args)


def get_question_detail(conn: sqlite3.Connection, question_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if not row:
        return None
    question = row_to_question(row)
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (question["run_id"],)).fetchone()
    if not run:
        return None

    unit_ids = question["source_units"]
    units: list[dict[str, Any]] = []
    if unit_ids:
        placeholders = ",".join("?" for _ in unit_ids)
        unit_rows = conn.execute(
            f"SELECT * FROM document_units WHERE run_id = ? AND unit_id IN ({placeholders}) ORDER BY unit_order",
            (question["run_id"], *unit_ids),
        ).fetchall()
        for unit in unit_rows:
            units.append(
                {
                    "unit_id": unit["unit_id"],
                    "page": unit["page"],
                    "bbox": json.loads(unit["bbox_json"]),
                    "text": unit["text"],
                }
            )

    figure_ids = question["figures"]
    figures: list[dict[str, Any]] = []
    if figure_ids:
        placeholders = ",".join("?" for _ in figure_ids)
        figure_rows = conn.execute(
            f"SELECT * FROM figures WHERE run_id = ? AND figure_id IN ({placeholders}) ORDER BY page, figure_id",
            (question["run_id"], *figure_ids),
        ).fetchall()
        for fig in figure_rows:
            figures.append(
                {
                    "figure_id": fig["figure_id"],
                    "page": fig["page"],
                    "bbox": json.loads(fig["bbox_json"]),
                    "path": fig["path"],
                    "url": artifact_url(question["run_id"], "paper/" + fig["path"].replace("\\", "/")),
                }
            )

    run_dir = Path(run["run_dir"])
    paper_blocks_path = run_dir / "paper" / "ocr_blocks.json"
    pages: list[dict[str, Any]] = []
    if paper_blocks_path.exists():
        paper_blocks = read_json(paper_blocks_path)
        page_map = {page["page"]: page for page in paper_blocks.get("pages", [])}
        for page_no in question["source_pages"]:
            page = page_map.get(page_no)
            if page:
                pages.append(
                    {
                        "page": page_no,
                        "url": artifact_url(question["run_id"], "paper/" + page["image"].replace("\\", "/")),
                        "width_pt": page.get("width_pt"),
                        "height_pt": page.get("height_pt"),
                    }
                )

    answer_source = load_answer_source(run_dir, int(question["question_no"]))
    answer_units: list[dict[str, Any]] = []
    answer_pages: list[dict[str, Any]] = []
    if answer_source:
        answer_blocks_path = run_dir / "answer" / "ocr_blocks.json"
        if answer_blocks_path.exists():
            answer_blocks = read_json(answer_blocks_path)
            answer_unit_ids = set(answer_source.get("source_units", []))
            for unit in answer_blocks.get("text_units", []):
                if unit.get("unit_id") in answer_unit_ids:
                    answer_units.append(
                        {
                            "unit_id": unit["unit_id"],
                            "page": unit["page"],
                            "bbox": unit["bbox"],
                            "text": unit["text"],
                        }
                    )
            answer_units.sort(key=lambda unit: unit["unit_id"])
            answer_page_map = {page["page"]: page for page in answer_blocks.get("pages", [])}
            for page_no in answer_source.get("source_pages", []):
                page = answer_page_map.get(page_no)
                if page:
                    answer_pages.append(
                        {
                            "page": page_no,
                            "url": artifact_url(question["run_id"], "answer/" + page["image"].replace("\\", "/")),
                            "width_pt": page.get("width_pt"),
                            "height_pt": page.get("height_pt"),
                        }
                    )

    question["source_units_detail"] = units
    question["figures_detail"] = figures
    question["option_images"] = derive_option_image_map(question)
    question["page_images"] = pages
    question["answer_source_detail"] = {
        "raw_text": answer_source.get("raw_text", "") if answer_source else "",
        "normalized_text": answer_source.get("normalized_text", "") if answer_source else "",
        "source_pages": answer_source.get("source_pages", []) if answer_source else [],
        "source_bboxes": answer_source.get("source_bboxes", {}) if answer_source else {},
        "source_units_detail": answer_units,
    }
    question["answer_page_images"] = answer_pages
    question["run"] = {
        "run_id": run["run_id"],
        "source_pdf": run["source_pdf"],
        "answer_pdf": run["answer_pdf"],
        "extractor": run["extractor"],
    }
    return question


def artifact_url(run_id: str, rel_path: str) -> str:
    return "/artifact/" + run_id + "/" + rel_path.replace("\\", "/")


def safe_artifact_path(conn: sqlite3.Connection, run_id: str, rel_path: str) -> Path | None:
    run = conn.execute("SELECT run_dir FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        return None
    root = Path(run["run_dir"]).resolve()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>题库审核台</title>
  <script>
    window.MathJax = {
      tex: {
        inlineMath: [['$', '$'], ['\\(', '\\)']],
        displayMath: [['$$', '$$'], ['\\[', '\\]']],
        processEscapes: true
      },
      options: {
        skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
      },
      startup: { typeset: false }
    };
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>
  <style>
    :root {
      --bg: #f3f5f7;
      --panel: #ffffff;
      --panel-alt: #f8fafc;
      --border: #d6dce5;
      --text: #1e293b;
      --muted: #64748b;
      --accent: #2563eb;
      --accent-soft: #e8f0ff;
      --ok: #087443;
      --warn: #a15c07;
      --bad: #b42318;
      --shadow: 0 1px 2px rgba(15, 23, 42, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      background: var(--bg);
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      font-size: 14px;
    }
    .shell {
      display: grid;
      grid-template-rows: 48px minmax(0, 1fr);
      height: 100vh;
      min-width: 1180px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 14px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
    }
    .brand { display: flex; align-items: baseline; gap: 10px; min-width: 300px; }
    .brand h1 { margin: 0; font-size: 18px; letter-spacing: 0; }
    .brand span { color: var(--muted); font-size: 12px; }
    .top-actions, .toolbar, .segmented { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .workspace {
      display: grid;
      grid-template-columns: 300px minmax(390px, 40vw) minmax(470px, 1fr);
      min-height: 0;
      height: 100%;
    }
    nav, aside, main {
      min-height: 0;
      overflow: auto;
      border-right: 1px solid var(--border);
      background: var(--panel);
    }
    nav { padding: 12px; }
    aside { padding: 12px; background: #eef2f6; }
    main { padding: 14px 16px 32px; border-right: 0; }
    h2 { margin: 0; font-size: 17px; }
    h3 { margin: 14px 0 7px; font-size: 13px; color: var(--muted); font-weight: 600; }
    button, select, input, textarea { font: inherit; }
    button {
      min-height: 34px;
      border: 1px solid var(--border);
      background: #fff;
      color: var(--text);
      border-radius: 6px;
      padding: 7px 10px;
      cursor: pointer;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    button.active { background: var(--accent-soft); border-color: #9db8f5; color: #1d4ed8; }
    button:disabled { opacity: .55; cursor: default; }
    input, select {
      min-height: 34px;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 7px 9px;
      background: #fff;
      color: var(--text);
    }
    input { width: 100%; }
    .filters { display: grid; gap: 8px; margin-bottom: 10px; }
    .filter-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .summary-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(70px, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }
    .metric {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel-alt);
      padding: 8px;
    }
    .metric b { display: block; font-size: 17px; }
    .metric span { color: var(--muted); font-size: 12px; }
    .q-list { display: grid; gap: 6px; }
    .q-item {
      border: 1px solid var(--border);
      background: #fff;
      border-radius: 8px;
      padding: 9px;
      text-align: left;
      box-shadow: var(--shadow);
    }
    .q-item.active { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(37, 99, 235, .13); }
    .q-title { display: flex; justify-content: space-between; gap: 8px; font-weight: 600; }
    .meta { color: var(--muted); font-size: 12px; margin-top: 3px; }
    .pill {
      display: inline-block;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 1px 7px;
      font-size: 12px;
      color: var(--muted);
      background: #f8fafc;
      margin-right: 4px;
      margin-top: 5px;
      line-height: 18px;
    }
    .pass, .human_verified, .published { color: var(--ok); border-color: #9fd2b8; background: #edf8f1; }
    .review, .pending { color: var(--warn); border-color: #ebc27d; background: #fff7e8; }
    .fail, .needs_review, .rejected { color: var(--bad); border-color: #ecaaa4; background: #fff0ef; }
    .source-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }
    .page-img, .figure-img {
      display: block;
      width: 100%;
      border: 1px solid var(--border);
      background: #fff;
      margin-bottom: 10px;
      border-radius: 6px;
    }
    .option-img-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 8px;
    }
    .option-img {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px;
      background: #fff;
    }
    .option-img img { max-width: 100%; display: block; margin-top: 5px; }
    .ocr-unit {
      border-left: 3px solid #9aa8ba;
      background: #fff;
      padding: 7px 9px;
      margin: 7px 0;
      word-break: break-word;
      white-space: pre-wrap;
      line-height: 1.45;
    }
    .editor-grid {
      display: grid;
      grid-template-columns: minmax(250px, 1fr) minmax(250px, 1fr);
      gap: 12px;
      align-items: start;
    }
    textarea {
      width: 100%;
      min-height: 176px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
      line-height: 1.45;
      resize: vertical;
      background: #fff;
      color: var(--text);
    }
    textarea.answer { min-height: 86px; }
    textarea.note { min-height: 76px; }
    .math-preview {
      min-height: 176px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
      line-height: 1.6;
      overflow: auto;
      word-break: break-word;
    }
    .math-preview.answer { min-height: 86px; }
    .math-preview .mjx-container { overflow-x: auto; overflow-y: hidden; max-width: 100%; }
    .panel {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      margin-top: 10px;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 9px;
      line-height: 1.45;
      font-family: Consolas, "Microsoft YaHei", monospace;
      font-size: 13px;
    }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .empty { color: var(--muted); padding: 16px; }
    .status-line { color: var(--muted); min-height: 18px; font-size: 12px; }
    .review-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
    }
    .review-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    @media (max-width: 1200px) {
      .shell { min-width: 820px; }
      .workspace { grid-template-columns: 280px 1fr; }
      aside { display: none; }
      .editor-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <h1>题库审核台</h1>
        <span id="runLabel">MinerU VLM · 题库入库审核</span>
      </div>
      <div class="top-actions">
        <span id="auditModelLabel" class="status-line"></span>
        <span id="statusLine" class="status-line"></span>
        <button id="reloadBtn">刷新</button>
        <button id="auditAllBtn">批量自动审核</button>
      </div>
    </header>
    <div class="workspace">
      <nav>
        <div class="summary-strip" id="summaryStrip"></div>
        <div class="filters">
          <select id="runFilter">
            <option value="">全部运行集</option>
          </select>
          <input id="searchInput" type="search" placeholder="搜索题号、题型、状态">
          <div class="filter-row">
            <select id="statusFilter">
              <option value="all">全部状态</option>
              <option value="pending">pending</option>
              <option value="needs_review">needs_review</option>
              <option value="human_verified">human_verified</option>
              <option value="published">published</option>
              <option value="rejected">rejected</option>
            </select>
            <select id="auditFilter">
              <option value="all">全部审核</option>
              <option value="pass">pass</option>
              <option value="review">review</option>
              <option value="fail">fail</option>
              <option value="not_audited">not_audited</option>
            </select>
          </div>
        </div>
        <div id="questionList" class="q-list"></div>
      </nav>
      <aside>
        <div class="source-head">
          <h2>来源对照</h2>
          <div class="segmented" id="sourceTabs">
            <button data-tab="pages" class="active">原页</button>
            <button data-tab="ocr">OCR</button>
            <button data-tab="figures">配图</button>
            <button data-tab="answer">答案</button>
          </div>
        </div>
        <div id="sourcePane" class="empty">选择一道题查看来源。</div>
      </aside>
      <main>
        <div id="editorPane" class="empty">选择一道题开始审核。</div>
      </main>
    </div>
  </div>
  <script>
    let runs = [];
    let questions = [];
    let currentId = null;
    let currentDetail = null;
    let selectedRunId = '';
    let sourceTab = 'pages';
    let auditConfig = {provider: 'pipeline_v2', vision_model: 'qwen3.6-27b', math_model: 'deepseek-v4-pro'};

    const $ = (id) => document.getElementById(id);

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    function mathHtml(value) {
      return escapeHtml(value).replace(/\n/g, '<br>');
    }

    function typesetElement(element) {
      if (!element || !window.MathJax) return;
      const run = () => window.MathJax.typesetPromise ? window.MathJax.typesetPromise([element]).catch(() => {}) : null;
      if (window.MathJax.startup?.promise) {
        window.MathJax.startup.promise.then(run);
      } else {
        run();
      }
    }

    function typesetAll() {
      document.querySelectorAll('.math-preview').forEach(typesetElement);
    }

    function pill(text, cls='') {
      return `<span class="pill ${cls}">${escapeHtml(text)}</span>`;
    }

    async function api(path, options={}) {
      const response = await fetch(path, {
        headers: {'Content-Type': 'application/json'},
        ...options
      });
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }

    function renderAuditConfig() {
      if (auditConfig.provider === 'pipeline_v2') {
        $('auditModelLabel').textContent = `Pipeline v2: ${auditConfig.vision_model || 'qwen'} + ${auditConfig.math_model || 'deepseek'}`;
      } else {
        $('auditModelLabel').textContent = `Auto audit: ${auditConfig.provider} / ${auditConfig.model || 'default'}`;
      }
    }

    async function loadAuditConfig() {
      auditConfig = await api('/api/audit-config');
      renderAuditConfig();
    }

    async function loadRuns() {
      const data = await api('/api/runs');
      runs = data.runs || [];
      const options = ['<option value="">全部运行集</option>'].concat(runs.map(run => {
        const label = `${run.run_id} · ${run.question_count}题 · ${run.audited_count}审`;
        return `<option value="${escapeHtml(run.run_id)}">${escapeHtml(label)}</option>`;
      }));
      $('runFilter').innerHTML = options.join('');
      $('runFilter').value = selectedRunId;
    }

    function filteredQuestions() {
      const status = $('statusFilter').value;
      const audit = $('auditFilter').value;
      const query = $('searchInput').value.trim().toLowerCase();
      return questions.filter(q => {
        const verdict = q.auto_audit?.verdict || 'not_audited';
        if (status !== 'all' && q.status !== status) return false;
        if (audit !== 'all' && verdict !== audit) return false;
        if (!query) return true;
        return (`${q.run_id} q${q.question_no} ${q.question_type} ${q.status} ${verdict}`).toLowerCase().includes(query);
      });
    }

    function renderSummary() {
      const total = questions.length;
      const pass = questions.filter(q => q.auto_audit?.verdict === 'pass').length;
      const review = questions.filter(q => q.auto_audit?.verdict === 'review').length;
      const verified = questions.filter(q => q.status === 'human_verified' || q.status === 'published').length;
      $('summaryStrip').innerHTML = [
        ['题目', total],
        ['自动通过', pass],
        ['待复核', review],
        ['人工确认', verified]
      ].map(([label, value]) => `<div class="metric"><b>${value}</b><span>${label}</span></div>`).join('');
    }

    async function loadQuestions() {
      const qs = selectedRunId ? `?run_id=${encodeURIComponent(selectedRunId)}` : '';
      const data = await api('/api/questions' + qs);
      questions = data.questions;
      if (currentId && !questions.some(q => q.id === currentId)) {
        currentId = null;
        currentDetail = null;
        $('sourcePane').innerHTML = '<div class="empty">选择一道题查看来源。</div>';
        $('editorPane').innerHTML = '<div class="empty">选择一道题开始审核。</div>';
      }
      renderSummary();
      renderList();
      if (!currentId && questions.length) {
        await selectQuestion(questions[0].id);
      }
    }

    function renderList() {
      const rows = filteredQuestions();
      $('questionList').innerHTML = rows.length ? rows.map(q => {
        const audit = q.auto_audit?.verdict || 'not_audited';
        const cls = q.status === 'needs_review' ? 'needs_review' : audit;
        return `<button class="q-item ${q.id === currentId ? 'active' : ''}" data-id="${q.id}">
          <div class="q-title"><span>Q${q.question_no}</span><span>${escapeHtml(q.question_type)}</span></div>
          <div class="meta">${escapeHtml(q.run_id)} · ${escapeHtml(q.status)} · ${escapeHtml(audit)}</div>
          <div>${pill(q.flags.length ? q.flags.join(', ') : 'no flags', q.flags.length ? 'review' : 'pass')} ${pill(audit, cls)}</div>
        </button>`;
      }).join('') : '<div class="empty">没有符合筛选条件的题目。</div>';
      for (const button of document.querySelectorAll('.q-item')) {
        button.addEventListener('click', () => selectQuestion(Number(button.dataset.id)));
      }
    }

    async function selectQuestion(id) {
      currentId = id;
      currentDetail = await api(`/api/questions/${id}`);
      $('runLabel').textContent = `${currentDetail.run.extractor} · ${currentDetail.run.run_id}`;
      renderList();
      renderSource(currentDetail);
      renderEditor(currentDetail);
    }

    function renderSource(q) {
      for (const button of document.querySelectorAll('#sourceTabs button')) {
        button.classList.toggle('active', button.dataset.tab === sourceTab);
      }
      let html = '';
      if (sourceTab === 'pages') {
        html = q.page_images.map(p => `
          <h3>第 ${p.page} 页</h3>
          <img class="page-img" src="${p.url}" alt="page ${p.page}">
        `).join('') || '<div class="empty">没有原页图。</div>';
      } else if (sourceTab === 'ocr') {
        html = q.source_units_detail.map(u => `
          <div class="ocr-unit"><b>${escapeHtml(u.unit_id)}</b> · page ${u.page}<br>${escapeHtml(u.text)}</div>
        `).join('') || '<div class="empty">没有 OCR block。</div>';
      } else if (sourceTab === 'figures') {
        html = q.figures_detail.map(f => `
          <h3>${escapeHtml(f.figure_id)} · page ${f.page}</h3>
          <img class="figure-img" src="${f.url}" alt="${escapeHtml(f.figure_id)}">
        `).join('') || '<div class="empty">没有配图候选。</div>';
      } else {
        const answer = q.answer_source_detail || {};
        const units = answer.source_units_detail || [];
        const pages = q.answer_page_images || [];
        html = `
          <h3>答案 OCR</h3>
          <pre>${escapeHtml(answer.normalized_text || answer.raw_text || '没有匹配到答案 OCR。')}</pre>
          ${units.map(u => `<div class="ocr-unit"><b>${escapeHtml(u.unit_id)}</b> · page ${u.page}<br>${escapeHtml(u.text)}</div>`).join('')}
          ${pages.map(p => `<h3>答案页 ${p.page}</h3><img class="page-img" src="${p.url}" alt="answer page ${p.page}">`).join('')}
        `;
      }
      $('sourcePane').innerHTML = html;
    }

    function renderOptions(q) {
      const labels = Object.keys(q.options || {});
      const imageLabels = Object.keys(q.option_images || {});
      if (!labels.length && !imageLabels.length) return '';
      return `<div class="panel">
        <h3>选项渲染</h3>
        ${labels.sort().map(label => `<div><b>${escapeHtml(label)}.</b> <span class="math-inline">${mathHtml(q.options[label])}</span></div>`).join('')}
        ${imageLabels.length ? `<div class="option-img-grid">${imageLabels.sort().map(label => {
          const fig = q.option_images[label];
          return `<div class="option-img"><b>${escapeHtml(label)}.</b><img src="${fig.url}" alt="option ${escapeHtml(label)}"></div>`;
        }).join('')}</div>` : ''}
      </div>`;
    }

    function renderAuditResult(audit) {
      if (!audit) return '<h3>自动审核结果</h3><pre>尚未执行</pre>';
      const stages = audit.stages || {};
      if (audit.provider === 'pipeline_v2_merge') {
        const vision = stages.qwen_vision || {};
        const math = stages.deepseek_math || {};
        const answer = stages.answer_split || {};
        return `
          <h3>Pipeline v2 审核</h3>
          <div class="summary-strip">
            <div class="metric"><b>${escapeHtml(audit.verdict || 'unknown')}</b><span>最终结论</span></div>
            <div class="metric"><b>${escapeHtml(vision.verdict || 'missing')}</b><span>Qwen Vision</span></div>
            <div class="metric"><b>${escapeHtml(math.math_verdict || 'missing')}</b><span>DeepSeek Math</span></div>
          </div>
          <pre>${escapeHtml(audit.summary || '')}</pre>
          <h3>答案拆分</h3>
          <pre>${escapeHtml(JSON.stringify({
            answer_final: answer.answer_final || '',
            has_analysis: Boolean(answer.answer_analysis),
            has_rubric: Boolean(answer.answer_rubric),
            issues: answer.issues || []
          }, null, 2))}</pre>
          <h3>完整 JSON</h3>
          <pre>${escapeHtml(JSON.stringify(audit, null, 2))}</pre>
        `;
      }
      return `
        <h3>自动审核结果</h3>
        <pre>${escapeHtml(JSON.stringify(audit, null, 2))}</pre>
      `;
    }

    function renderEditor(q) {
      const audit = q.auto_audit;
      const auditHtml = renderAuditResult(audit);
      $('editorPane').innerHTML = `
        <div class="review-header">
          <div class="review-title">
            <h2>Q${q.question_no}</h2>
            ${pill(q.question_type)}
            ${pill('confidence ' + q.confidence)}
            ${(q.flags || []).map(flag => pill(flag, 'review')).join('')}
            ${pill(audit?.verdict || 'not_audited', audit?.verdict || '')}
          </div>
          <div class="toolbar">
            <button class="primary" id="saveBtn">保存审核</button>
            <button id="auditBtn">自动审核本题</button>
          </div>
        </div>
        <div class="editor-grid">
          <section>
            <h3>题干 / LaTeX 源码</h3>
            <textarea id="stemInput">${escapeHtml(q.display_text)}</textarea>
          </section>
          <section>
            <h3>题干渲染预览</h3>
            <div id="stemPreview" class="math-preview"></div>
          </section>
        </div>
        ${renderOptions(q)}
        <div class="editor-grid">
          <section>
            <h3>答案源码</h3>
            <textarea id="answerInput" class="answer">${escapeHtml(q.display_answer)}</textarea>
          </section>
          <section>
            <h3>答案渲染预览</h3>
            <div id="answerPreview" class="math-preview answer"></div>
          </section>
        </div>
        <div class="split">
          <div>
            <h3>审核状态</h3>
            <select id="statusInput">
              ${['pending','needs_review','human_verified','published','rejected'].map(s => `<option value="${s}" ${q.status === s ? 'selected' : ''}>${s}</option>`).join('')}
            </select>
          </div>
          <div>
            <h3>来源</h3>
            <pre>${escapeHtml(q.run.extractor)} · ${escapeHtml(q.run.run_id)}</pre>
          </div>
        </div>
        <h3>反馈备注</h3>
        <textarea id="noteInput" class="note">${escapeHtml(q.reviewer_note)}</textarea>
        <div class="panel">${auditHtml}</div>
      `;
      $('saveBtn').addEventListener('click', saveCurrent);
      $('auditBtn').addEventListener('click', auditCurrent);
      $('stemInput').addEventListener('input', updatePreviews);
      $('answerInput').addEventListener('input', updatePreviews);
      updatePreviews();
      document.querySelectorAll('.math-inline').forEach(typesetElement);
    }

    function updatePreviews() {
      const stem = $('stemInput');
      const answer = $('answerInput');
      if (stem) {
        const preview = $('stemPreview');
        preview.innerHTML = mathHtml(stem.value);
        typesetElement(preview);
      }
      if (answer) {
        const preview = $('answerPreview');
        preview.innerHTML = mathHtml(answer.value);
        typesetElement(preview);
      }
    }

    async function saveCurrent() {
      if (!currentId) return;
      $('statusLine').textContent = '保存中...';
      const payload = {
        edited_text: $('stemInput').value,
        edited_answer: $('answerInput').value,
        status: $('statusInput').value,
        reviewer_note: $('noteInput').value
      };
      const updated = await api(`/api/questions/${currentId}/review`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
      $('statusLine').textContent = '已保存 ' + updated.question.updated_at;
      await loadQuestions();
      await selectQuestion(currentId);
    }

    async function auditCurrent() {
      if (!currentId) return;
      $('statusLine').textContent = 'Pipeline v2 审核中...';
      const updated = await api(`/api/questions/${currentId}/audit`, {method: 'POST', body: '{}'});
      $('statusLine').textContent = 'Pipeline v2 完成：' + updated.audit.verdict;
      await loadQuestions();
      await selectQuestion(currentId);
    }

    for (const button of document.querySelectorAll('#sourceTabs button')) {
      button.addEventListener('click', () => {
        sourceTab = button.dataset.tab;
        if (currentDetail) renderSource(currentDetail);
      });
    }
    $('reloadBtn').addEventListener('click', loadQuestions);
    $('searchInput').addEventListener('input', renderList);
    $('runFilter').addEventListener('change', async () => {
      selectedRunId = $('runFilter').value;
      currentId = null;
      currentDetail = null;
      await loadQuestions();
    });
    $('statusFilter').addEventListener('change', renderList);
    $('auditFilter').addEventListener('change', renderList);
    $('auditAllBtn').addEventListener('click', async () => {
      if (!selectedRunId) {
        $('statusLine').textContent = '请先选择一个运行集再批量审核。';
        return;
      }
      $('statusLine').textContent = `Pipeline v2 批量审核中：${selectedRunId}`;
      const result = await api('/api/audit-all', {
        method: 'POST',
        body: JSON.stringify({run_id: selectedRunId})
      });
      $('statusLine').textContent = `Pipeline v2 批量完成：${result.planned_questions || 0} 题`;
      await loadQuestions();
      if (currentId) await selectQuestion(currentId);
    });
    window.addEventListener('load', typesetAll);
    Promise.all([loadAuditConfig(), loadRuns()]).then(loadQuestions).catch(err => {
      $('editorPane').innerHTML = `<pre>${escapeHtml(err.stack || err.message)}</pre>`;
    });
  </script>
</body>
</html>
"""


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "QuestionBankReview/0.1"

    @property
    def app(self) -> "ReviewServer":
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return
        if parsed.path == "/api/audit-config":
            write_json_response(
                self,
                {
                    "provider": self.app.audit_provider or "pipeline_v2",
                    "model": self.app.audit_model or DEFAULT_BAILIAN_MODEL,
                    "vision_model": self.app.audit_model or DEFAULT_BAILIAN_MODEL,
                    "math_model": self.app.math_model or DEFAULT_V2_MATH_MODEL,
                    "math_scope": self.app.math_scope,
                },
            )
            return
        if parsed.path == "/api/runs":
            self.handle_runs()
            return
        if parsed.path == "/api/questions":
            self.handle_questions()
            return
        if parsed.path.startswith("/api/questions/"):
            self.handle_question_detail(parsed.path)
            return
        if parsed.path.startswith("/artifact/"):
            self.handle_artifact(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/audit-all":
            payload = self.read_body_json()
            requested_run_id = (payload.get("run_id") or self.app.run_id or "").strip()
            if (self.app.audit_provider or "pipeline_v2") == "pipeline_v2":
                if not requested_run_id and not payload.get("allow_all"):
                    self.send_error(HTTPStatus.BAD_REQUEST, "Select one run_id for Pipeline v2 batch audit, or pass allow_all=true.")
                    return
                max_questions = payload.get("max_questions")
                result = run_pipeline_v2_batch_audit(
                    self.app.db_path,
                    requested_run_id or None,
                    vision_model=self.app.audit_model,
                    math_model=self.app.math_model,
                    math_scope=self.app.math_scope,
                    timeout=self.app.audit_timeout,
                    max_questions=int(max_questions) if max_questions else None,
                )
            else:
                result = run_audit(self.app.db_path, requested_run_id or None, provider=self.app.audit_provider, model=self.app.audit_model)
            write_json_response(self, result)
            return
        if parsed.path.startswith("/api/questions/") and parsed.path.endswith("/review"):
            self.handle_review_update(parsed.path)
            return
        if parsed.path.startswith("/api/questions/") and parsed.path.endswith("/audit"):
            self.handle_single_audit(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def handle_runs(self) -> None:
        conn = connect_db(self.app.db_path)
        rows = conn.execute(
            """
            SELECT r.run_id, r.extractor, r.imported_at,
                   COUNT(q.id) AS question_count,
                   SUM(CASE WHEN q.auto_audit_json IS NOT NULL AND q.auto_audit_json != '' THEN 1 ELSE 0 END) AS audited_count
            FROM runs r
            LEFT JOIN questions q ON q.run_id = r.run_id
            GROUP BY r.run_id
            ORDER BY r.run_id
            """
        ).fetchall()
        conn.close()
        runs = [
            {
                "run_id": row["run_id"],
                "extractor": row["extractor"],
                "imported_at": row["imported_at"],
                "question_count": row["question_count"],
                "audited_count": row["audited_count"],
            }
            for row in rows
        ]
        write_json_response(self, {"runs": runs})

    def handle_questions(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        requested_run_id = (query.get("run_id") or [""])[0].strip()
        run_id = self.app.run_id or requested_run_id
        conn = connect_db(self.app.db_path)
        params: tuple[Any, ...] = ()
        where = ""
        if run_id:
            where = "WHERE run_id = ?"
            params = (run_id,)
        rows = conn.execute(f"SELECT * FROM questions {where} ORDER BY run_id, question_no", params).fetchall()
        questions = []
        for row in rows:
            q = row_to_question(row)
            questions.append(
                {
                    "id": q["id"],
                    "run_id": q["run_id"],
                    "question_no": q["question_no"],
                    "question_type": q["question_type"],
                    "status": q["status"],
                    "flags": q["flags"],
                    "confidence": q["confidence"],
                    "auto_audit": q["auto_audit"],
                }
            )
        conn.close()
        write_json_response(self, {"count": len(questions), "questions": questions})

    def handle_question_detail(self, path: str) -> None:
        match = re.fullmatch(r"/api/questions/(\d+)", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        question_id = int(match.group(1))
        conn = connect_db(self.app.db_path)
        detail = get_question_detail(conn, question_id)
        conn.close()
        if not detail:
            self.send_error(HTTPStatus.NOT_FOUND, "Question not found")
            return
        write_json_response(self, detail)

    def handle_review_update(self, path: str) -> None:
        match = re.fullmatch(r"/api/questions/(\d+)/review", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        question_id = int(match.group(1))
        payload = self.read_body_json()
        allowed_status = {"pending", "needs_review", "human_verified", "published", "rejected"}
        status = payload.get("status", "pending")
        if status not in allowed_status:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid status")
            return
        conn = connect_db(self.app.db_path)
        with conn:
            conn.execute(
                """
                UPDATE questions
                SET edited_text = ?, edited_answer = ?, status = ?, reviewer_note = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.get("edited_text"),
                    payload.get("edited_answer"),
                    status,
                    payload.get("reviewer_note", ""),
                    utc_now(),
                    question_id,
                ),
            )
            conn.execute(
                "INSERT INTO review_events(question_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (question_id, "human_review", json_text(payload), utc_now()),
            )
        detail = get_question_detail(conn, question_id)
        conn.close()
        write_json_response(self, {"question": detail})

    def handle_single_audit(self, path: str) -> None:
        match = re.fullmatch(r"/api/questions/(\d+)/audit", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        question_id = int(match.group(1))
        conn = connect_db(self.app.db_path)
        detail = get_question_detail(conn, question_id)
        if not detail:
            conn.close()
            self.send_error(HTTPStatus.NOT_FOUND, "Question not found")
            return
        if (self.app.audit_provider or "pipeline_v2") == "pipeline_v2":
            conn.close()
            result = run_pipeline_v2_single_audit(
                self.app.db_path,
                detail["run_id"],
                int(detail["question_no"]),
                vision_model=self.app.audit_model,
                math_model=self.app.math_model,
                math_scope=self.app.math_scope,
                timeout=self.app.audit_timeout,
            )
            write_json_response(self, result)
            return
        audit = audit_question(detail, provider=self.app.audit_provider, model=self.app.audit_model)
        with conn:
            conn.execute(
                "UPDATE questions SET auto_audit_json = ?, updated_at = ? WHERE id = ?",
                (json_text(audit), utc_now(), question_id),
            )
            conn.execute(
                "INSERT INTO review_events(question_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (question_id, "auto_audit", json_text(audit), utc_now()),
            )
        conn.close()
        write_json_response(self, {"audit": audit})

    def handle_artifact(self, path: str) -> None:
        parts = path.split("/", 3)
        if len(parts) != 4:
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return
        run_id = unquote(parts[2])
        rel_path = unquote(parts[3])
        conn = connect_db(self.app.db_path)
        target = safe_artifact_path(conn, run_id, rel_path)
        conn.close()
        if not target or not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))


class ReviewServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        db_path: Path,
        run_id: str | None,
        audit_provider: str | None = None,
        audit_model: str | None = None,
        math_model: str | None = None,
        math_scope: str = DEFAULT_V2_MATH_SCOPE,
        audit_timeout: int = DEFAULT_V2_TIMEOUT,
    ):
        super().__init__(server_address, ReviewHandler)
        self.db_path = db_path
        self.run_id = run_id
        self.audit_provider = audit_provider
        self.audit_model = audit_model
        self.math_model = math_model
        self.math_scope = math_scope
        self.audit_timeout = audit_timeout


def serve(
    db_path: Path,
    host: str,
    port: int,
    run_id: str | None,
    audit_provider: str | None = None,
    audit_model: str | None = None,
    math_model: str | None = None,
    math_scope: str = DEFAULT_V2_MATH_SCOPE,
    audit_timeout: int = DEFAULT_V2_TIMEOUT,
) -> None:
    conn = connect_db(db_path)
    init_db(conn)
    count = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    conn.close()
    if count == 0:
        raise SystemExit(f"No questions in database: {db_path}")
    server = ReviewServer(
        (host, port),
        db_path,
        run_id,
        audit_provider=audit_provider,
        audit_model=audit_model,
        math_model=math_model,
        math_scope=math_scope,
        audit_timeout=audit_timeout,
    )
    print(f"Question bank review server: http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server")
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SQLite question bank and review UI for parsed math exams.")
    parser.add_argument("--db", default="code/output/question_bank.sqlite", help="SQLite database path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-run", help="Import a pipeline run into the question bank.")
    import_parser.add_argument("--run-dir", required=True, help="Pipeline run directory.")
    import_parser.add_argument("--run-id", help="Override run id.")
    import_parser.add_argument("--no-replace", action="store_true", help="Do not replace an existing run.")
    import_parser.add_argument("--audit", action="store_true", help="Run automatic audit after import.")
    import_parser.add_argument("--audit-provider", default="qwen", choices=["qwen", "rule"], help="Automatic audit provider.")
    import_parser.add_argument("--audit-model", default=DEFAULT_BAILIAN_MODEL, help="Qwen/Bailian model name.")

    audit_parser = subparsers.add_parser("audit", help="Run automatic audit for existing questions.")
    audit_parser.add_argument("--run-id", help="Only audit one run.")
    audit_parser.add_argument("--question-id", type=int, help="Only audit one database question id.")
    audit_parser.add_argument("--question-no", type=int, help="Only audit one question number.")
    audit_parser.add_argument("--audit-provider", default="qwen", choices=["qwen", "rule"], help="Automatic audit provider.")
    audit_parser.add_argument("--audit-model", default=DEFAULT_BAILIAN_MODEL, help="Qwen/Bailian model name.")

    serve_parser = subparsers.add_parser("serve", help="Start local review web UI.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--run-id", help="Only show one run.")
    serve_parser.add_argument("--audit-provider", default="pipeline_v2", choices=["pipeline_v2", "qwen", "rule"], help="Automatic audit provider.")
    serve_parser.add_argument("--audit-model", default=DEFAULT_BAILIAN_MODEL, help="Qwen/Bailian vision model name.")
    serve_parser.add_argument("--math-model", default=DEFAULT_V2_MATH_MODEL, help="Bailian math audit model name.")
    serve_parser.add_argument("--math-scope", default=DEFAULT_V2_MATH_SCOPE, choices=["all", "none"], help="Whether Pipeline v2 should run the math audit stage.")
    serve_parser.add_argument("--audit-timeout", type=int, default=DEFAULT_V2_TIMEOUT, help="Per-model call timeout in seconds.")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    db_path = Path(args.db)
    if args.command == "import-run":
        run_id = import_run(db_path, Path(args.run_dir), args.run_id, replace=not args.no_replace)
        print(f"Imported run {run_id} into {db_path}")
        if args.audit:
            print(json.dumps(run_audit(db_path, run_id, provider=args.audit_provider, model=args.audit_model), ensure_ascii=False))
    elif args.command == "audit":
        print(
            json.dumps(
                run_audit(
                    db_path,
                    args.run_id,
                    provider=args.audit_provider,
                    model=args.audit_model,
                    question_id=args.question_id,
                    question_no=args.question_no,
                ),
                ensure_ascii=False,
            )
        )
    elif args.command == "serve":
        serve(
            db_path,
            args.host,
            args.port,
            args.run_id,
            audit_provider=args.audit_provider,
            audit_model=args.audit_model,
            math_model=args.math_model,
            math_scope=args.math_scope,
            audit_timeout=args.audit_timeout,
        )


if __name__ == "__main__":
    main()
