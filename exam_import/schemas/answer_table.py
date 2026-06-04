from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import ValidationError, expect_float, expect_list, expect_mapping, expect_optional_string, expect_string, expect_string_list, read_json_file


@dataclass(frozen=True)
class AnswerTableEntry:
    question_no: int
    answer_markdown: list[str]
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AnswerTableEntry":
        question_no = payload.get("question_no")
        if not isinstance(question_no, int) or question_no < 1:
            raise ValidationError("entries[].question_no must be a positive integer")
        return cls(
            question_no=question_no,
            answer_markdown=expect_string_list(payload, "answer_markdown", allow_empty=False),
            confidence=expect_float(payload, "confidence", default=1.0),
            reason=expect_string(payload, "reason"),
        )


@dataclass(frozen=True)
class AnswerTableResult:
    label: str
    role: str
    target_field: str
    entries: list[AnswerTableEntry]
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "role": self.role,
            "target_field": self.target_field,
            "entries": [item.to_dict() for item in self.entries],
            "confidence": self.confidence,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AnswerTableResult":
        role = expect_string(payload, "role")
        if role not in {"answer_key_table", "analysis_table", "noise", "uncertain"}:
            raise ValidationError(f"Unsupported table role: {role}")
        target_field = expect_string(payload, "target_field")
        if target_field not in {"answer_markdown", "analysis_markdown", "none"}:
            raise ValidationError(f"Unsupported table target_field: {target_field}")
        entries_payload = expect_list(payload, "entries")
        entries = [
            AnswerTableEntry.from_dict(expect_mapping(item, f"entries[{index}]"))
            for index, item in enumerate(entries_payload)
        ]
        if role == "answer_key_table" and target_field != "answer_markdown":
            raise ValidationError("answer_key_table target_field must be answer_markdown")
        if role == "analysis_table" and target_field != "analysis_markdown":
            raise ValidationError("analysis_table target_field must be analysis_markdown")
        if role in {"noise", "uncertain"} and target_field != "none":
            raise ValidationError(f"{role} target_field must be none")
        if role != "answer_key_table" and entries:
            raise ValidationError(f"{role} must not contain entries")
        return cls(
            label=expect_string(payload, "label"),
            role=role,
            target_field=target_field,
            entries=entries,
            confidence=expect_float(payload, "confidence", default=1.0),
            reason=expect_string(payload, "reason"),
        )


@dataclass(frozen=True)
class AnswerTableRisk:
    label: str
    severity: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AnswerTableRisk":
        severity = expect_string(payload, "severity")
        if severity not in {"info", "warning", "error"}:
            raise ValidationError(f"Unsupported table risk severity: {severity}")
        return cls(
            label=expect_string(payload, "label"),
            severity=severity,
            reason=expect_string(payload, "reason"),
        )


@dataclass(frozen=True)
class AnswerTableReview:
    tables: list[AnswerTableResult]
    risks: list[AnswerTableRisk]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables": [item.to_dict() for item in self.tables],
            "risks": [item.to_dict() for item in self.risks],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AnswerTableReview":
        tables_payload = expect_list(payload, "tables")
        risks_payload = expect_list(payload, "risks")
        return cls(
            tables=[
                AnswerTableResult.from_dict(expect_mapping(item, f"tables[{index}]"))
                for index, item in enumerate(tables_payload)
            ],
            risks=[
                AnswerTableRisk.from_dict(expect_mapping(item, f"risks[{index}]"))
                for index, item in enumerate(risks_payload)
            ],
        )


def load_answer_table_review(path: Path) -> AnswerTableReview:
    payload = expect_mapping(read_json_file(path), str(path))
    return AnswerTableReview.from_dict(payload)
