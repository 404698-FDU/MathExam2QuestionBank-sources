from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[3]
WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import exam_agent_pipeline as eap
import question_bank_app as qba
from common_io import read_json, write_json


def answer_ocr_text(run_dir: Path) -> str:
    full_md = run_dir / "answer" / "mineru_extract" / "full.md"
    if full_md.exists():
        return full_md.read_text(encoding="utf-8", errors="replace")
    raw = read_json(run_dir / "answers_raw.json", [])
    if isinstance(raw, list):
        parts = []
        for item in raw:
            question_no = item.get("question_no", "?")
            text = item.get("normalized_text") or item.get("raw_text") or ""
            parts.append(f"## OCR block {question_no}\n{text}")
        return "\n\n".join(parts)
    raise SystemExit(f"No answer OCR text found under {run_dir}")


def build_messages(ocr_text: str, expected_count: int) -> list[dict[str, str]]:
    system = (
        "你是数学试卷答案页的结构化抽取器。你只根据输入 OCR 文本抽取答案，不补题、不改题、不过度推断。"
        "输入可能来自图片 OCR，可能有表格、乱码页眉、评分说明、分值行和换行错误。"
        "目标是为题库生成每题答案记录。"
    )
    user = f"""/no_think
请从下面的答案页 OCR 中抽取第 1 至第 {expected_count} 题的答案。

要求：
1. 只返回 JSON 对象，不要 Markdown。
2. JSON schema:
{{
  "answers": [
    {{
      "question_no": 1,
      "answer_final": "最终答案；选择题用 A/B/C/D；填空题用简短答案；解答题可为空",
      "answer_analysis": "解析或评分标准原文摘要；没有则为空字符串",
      "raw_span": "支撑该答案的 OCR 原文片段",
      "confidence": 0.0,
      "needs_review_reason": "低置信度或缺失原因；没有则为空字符串"
    }}
  ],
  "missing_question_numbers": [],
  "notes": []
}}
3. 如果某题答案缺失或无法可靠抽取，也要在 missing_question_numbers 中列出。
4. 选择题答案表格也要展开成单题记录。
5. 保留数学符号和 LaTeX，不要把公式翻译成自然语言。

答案页 OCR：
{ocr_text}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def normalize_payload(payload: dict[str, Any], expected_count: int) -> dict[str, Any]:
    answers = payload.get("answers", [])
    if not isinstance(answers, list):
        answers = []
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in answers:
        if not isinstance(item, dict):
            continue
        try:
            question_no = int(item.get("question_no"))
        except (TypeError, ValueError):
            continue
        if not 1 <= question_no <= expected_count or question_no in seen:
            continue
        seen.add(question_no)
        normalized.append(
            {
                "question_no": question_no,
                "answer_final": str(item.get("answer_final") or "").strip(),
                "answer_analysis": str(item.get("answer_analysis") or "").strip(),
                "raw_span": str(item.get("raw_span") or "").strip(),
                "confidence": item.get("confidence"),
                "needs_review_reason": str(item.get("needs_review_reason") or "").strip(),
            }
        )
    normalized.sort(key=lambda item: item["question_no"])
    missing = [number for number in range(1, expected_count + 1) if number not in seen]
    return {
        "answers": normalized,
        "missing_question_numbers": missing,
        "notes": payload.get("notes", []) if isinstance(payload.get("notes", []), list) else [str(payload.get("notes"))],
    }


def answer_map_from_payload(payload: dict[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for item in payload.get("answers", []):
        question_no = int(item["question_no"])
        parts = []
        if item.get("answer_final"):
            parts.append(f"答案：{item['answer_final']}")
        if item.get("answer_analysis"):
            parts.append(str(item["answer_analysis"]))
        elif item.get("raw_span"):
            parts.append(str(item["raw_span"]))
        if parts:
            result[question_no] = "\n".join(parts).strip()
    return result


def validate_with_answers(run_dir: Path, answers: dict[int, str]) -> dict[str, Any]:
    questions = [eap.Question(**item) for item in read_json(run_dir / "questions_normalized.json", [])]
    validation = eap.validate_questions(questions, answers)
    return validation


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir)
    expected_count = args.expected_count
    ocr_text = answer_ocr_text(run_dir)
    raw_content = qba.call_bailian_chat(build_messages(ocr_text, expected_count), args.model, timeout=args.timeout)
    payload = normalize_payload(qba.extract_json_object(raw_content), expected_count)
    answers = answer_map_from_payload(payload)
    validation = validate_with_answers(run_dir, answers)
    out_dir = Path(args.out_dir) if args.out_dir else run_dir / "qwen_answer_extract"
    write_json(out_dir / "qwen_answer_extract_result.json", payload)
    write_json(out_dir / "answers_by_question.qwen.json", {str(key): value for key, value in sorted(answers.items())})
    write_json(out_dir / "validation_with_qwen_answers.json", validation)
    if args.apply:
        backup = run_dir / "answers_by_question.before_qwen_extract.json"
        current = read_json(run_dir / "answers_by_question.json", {})
        if not backup.exists():
            write_json(backup, current)
        write_json(run_dir / "answers_by_question.json", {str(key): value for key, value in sorted(answers.items())})
        write_json(run_dir / "validation_report.json", validation)
        questions_path = run_dir / "questions_normalized.json"
        questions_backup = run_dir / "questions_normalized.before_qwen_extract.json"
        questions = read_json(questions_path, [])
        if isinstance(questions, list):
            if not questions_backup.exists():
                write_json(questions_backup, questions)
            for question in questions:
                try:
                    question_no = int(question.get("question_no"))
                except (TypeError, ValueError):
                    continue
                if question_no not in answers:
                    continue
                question["answer_raw"] = answers[question_no]
                question["flags"] = [flag for flag in question.get("flags", []) if flag != "answer_not_matched"]
            write_json(questions_path, questions)
    summary = {
        "run_dir": str(run_dir),
        "model": args.model,
        "answer_count": len(answers),
        "missing_question_numbers": payload["missing_question_numbers"],
        "validation": validation,
        "applied": bool(args.apply),
        "out_dir": str(out_dir),
    }
    write_json(out_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract answer map from image-answer OCR using Bailian Qwen.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--model", default=qba.DEFAULT_BAILIAN_MODEL)
    parser.add_argument("--expected-count", type=int, default=21)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--out-dir")
    parser.add_argument("--apply", action="store_true")
    return parser


if __name__ == "__main__":
    print(json.dumps(run(build_parser().parse_args()), ensure_ascii=False, indent=2))
