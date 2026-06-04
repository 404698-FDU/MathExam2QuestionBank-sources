from __future__ import annotations

from pathlib import Path
from typing import Any

from exam_import.core.io import write_json, write_jsonl
from exam_import.schemas.question_record import QuestionRecord


def question_record_payloads(records: list[QuestionRecord]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in sorted(records, key=lambda item: item.question_no)]


def write_question_bank(question_bank_dir: Path, records: list[QuestionRecord]) -> list[dict[str, Any]]:
    payloads = question_record_payloads(records)
    write_json(question_bank_dir / "question_bank.json", payloads)
    write_jsonl(question_bank_dir / "question_bank.jsonl", payloads)
    return payloads
