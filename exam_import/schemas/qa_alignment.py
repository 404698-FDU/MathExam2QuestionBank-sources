from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import (
    IssueRecord,
    ValidationError,
    expect_bool,
    expect_enum,
    expect_float,
    expect_int,
    expect_issue_list,
    expect_list,
    expect_mapping,
    expect_optional_string,
    expect_string,
    expect_string_list,
    read_json_file,
)


QA_ALIGNMENT_SCHEMA_VERSION = "qa_alignment_v2"
ALIGNMENT_MODES = {"pure_paper", "mixed", "paper_plus_answer", "answer_patch"}


@dataclass(frozen=True)
class QuestionSide:
    source_part: str
    range_mode: str
    status: str
    labels: list[str]
    core_labels: list[str]
    surface_labels: list[str]
    visual_labels: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QuestionSide":
        return cls(
            source_part=expect_enum(payload, "source_part", {"paper", "mixed"}),
            range_mode=expect_enum(payload, "range_mode", {"pure_paper", "mixed"}),
            status=expect_enum(payload, "status", {"found", "missing"}),
            labels=expect_string_list(payload, "labels"),
            core_labels=expect_string_list(payload, "core_labels"),
            surface_labels=expect_string_list(payload, "surface_labels"),
            visual_labels=expect_string_list(payload, "visual_labels"),
        )


@dataclass(frozen=True)
class AnswerItem:
    role: str
    range_mode: str
    labels: list[str]
    span_text_excerpt: str
    continues_previous: bool
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AnswerItem":
        return cls(
            role=expect_enum(payload, "role", {"answer", "analysis", "rubric", "mixed_answer_analysis"}),
            range_mode=expect_enum(payload, "range_mode", {"pure_answer", "mixed"}),
            labels=expect_string_list(payload, "labels"),
            span_text_excerpt=expect_optional_string(payload, "span_text_excerpt"),
            continues_previous=expect_bool(payload, "continues_previous", default=False),
            confidence=expect_float(payload, "confidence", default=1.0),
            reason=expect_string(payload, "reason"),
        )


@dataclass(frozen=True)
class AnswerSide:
    status: str
    source_mode: str
    source_part: str
    embedded_in_question_range: bool
    items: list[AnswerItem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_mode": self.source_mode,
            "source_part": self.source_part,
            "embedded_in_question_range": self.embedded_in_question_range,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AnswerSide":
        items_payload = expect_list(payload, "items")
        return cls(
            status=expect_enum(payload, "status", {"pending_import", "found", "missing", "embedded"}),
            source_mode=expect_enum(payload, "source_mode", {"none", "pure_answer", "mixed", "answer_patch"}),
            source_part=expect_enum(payload, "source_part", {"none", "answer", "mixed"}),
            embedded_in_question_range=expect_bool(payload, "embedded_in_question_range"),
            items=[
                AnswerItem.from_dict(expect_mapping(item, f"items[{index}]"))
                for index, item in enumerate(items_payload)
            ],
        )


@dataclass(frozen=True)
class QAAlignmentRow:
    question_no: int
    question: QuestionSide
    answer: AnswerSide
    alignment_status: str
    issues: list[IssueRecord]

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_no": self.question_no,
            "question": self.question.to_dict(),
            "answer": self.answer.to_dict(),
            "alignment_status": self.alignment_status,
            "issues": [item.to_dict() for item in self.issues],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QAAlignmentRow":
        question_no = expect_int(payload, "question_no", minimum=1)
        return cls(
            question_no=question_no,
            question=QuestionSide.from_dict(expect_mapping(payload.get("question"), "question")),
            answer=AnswerSide.from_dict(expect_mapping(payload.get("answer"), "answer")),
            alignment_status=expect_enum(
                payload,
                "alignment_status",
                {"question_only", "question_with_answer", "mixed_embedded", "answer_patched", "needs_review"},
            ),
            issues=expect_issue_list(payload, "issues"),
        )


@dataclass(frozen=True)
class AnswerImport:
    status: str
    preserves_question_side: bool
    source_run_id: str
    answer_pdf: str
    patched_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AnswerImport":
        return cls(
            status=expect_enum(payload, "status", {"pending", "not_applicable", "initial_import", "patched"}),
            preserves_question_side=expect_bool(payload, "preserves_question_side"),
            source_run_id=expect_optional_string(payload, "source_run_id"),
            answer_pdf=expect_optional_string(payload, "answer_pdf"),
            patched_at=expect_optional_string(payload, "patched_at"),
        )


@dataclass(frozen=True)
class VisualAssetSummary:
    asset_count: int
    assigned_count: int
    noise_count: int
    uncertain_count: int
    risk_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VisualAssetSummary":
        return cls(
            asset_count=expect_int(payload, "asset_count", minimum=0),
            assigned_count=expect_int(payload, "assigned_count", minimum=0),
            noise_count=expect_int(payload, "noise_count", minimum=0),
            uncertain_count=expect_int(payload, "uncertain_count", minimum=0),
            risk_count=expect_int(payload, "risk_count", minimum=0),
        )


@dataclass(frozen=True)
class QAAlignmentDocument:
    schema_version: str
    run_id: str
    alignment_mode: str
    source_parts: list[str]
    qa_alignment: list[QAAlignmentRow]
    extra_answer_numbers: list[int]
    missing_answer_numbers: list[int]
    answer_import: AnswerImport
    visual_asset_assignment_summary: VisualAssetSummary
    asset_match_risk_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "alignment_mode": self.alignment_mode,
            "source_parts": self.source_parts,
            "qa_alignment": [item.to_dict() for item in self.qa_alignment],
            "extra_answer_numbers": self.extra_answer_numbers,
            "missing_answer_numbers": self.missing_answer_numbers,
            "answer_import": self.answer_import.to_dict(),
            "visual_asset_assignment_summary": self.visual_asset_assignment_summary.to_dict(),
            "asset_match_risk_count": self.asset_match_risk_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QAAlignmentDocument":
        schema_version = expect_string(payload, "schema_version")
        if schema_version != QA_ALIGNMENT_SCHEMA_VERSION:
            raise ValidationError(
                f"schema_version must be {QA_ALIGNMENT_SCHEMA_VERSION}, got {schema_version}"
            )
        rows_payload = expect_list(payload, "qa_alignment")
        if not rows_payload:
            raise ValidationError("qa_alignment must not be empty")
        source_parts = expect_string_list(payload, "source_parts", allow_empty=False)
        for item in source_parts:
            if item not in {"paper", "answer", "mixed"}:
                raise ValidationError(f"Invalid source_parts item: {item}")
        return cls(
            schema_version=schema_version,
            run_id=expect_string(payload, "run_id"),
            alignment_mode=expect_enum(payload, "alignment_mode", ALIGNMENT_MODES),
            source_parts=source_parts,
            qa_alignment=[
                QAAlignmentRow.from_dict(expect_mapping(item, f"qa_alignment[{index}]"))
                for index, item in enumerate(rows_payload)
            ],
            extra_answer_numbers=_load_int_list(payload, "extra_answer_numbers"),
            missing_answer_numbers=_load_int_list(payload, "missing_answer_numbers"),
            answer_import=AnswerImport.from_dict(expect_mapping(payload.get("answer_import"), "answer_import")),
            visual_asset_assignment_summary=VisualAssetSummary.from_dict(
                expect_mapping(payload.get("visual_asset_assignment_summary"), "visual_asset_assignment_summary")
            ),
            asset_match_risk_count=expect_int(payload, "asset_match_risk_count", minimum=0),
        )


def _load_int_list(payload: Mapping[str, Any], field_name: str) -> list[int]:
    values = expect_list(payload, field_name)
    result: list[int] = []
    for index, item in enumerate(values):
        if not isinstance(item, int) or item < 1:
            raise ValidationError(f"{field_name}[{index}] must be a positive integer")
        result.append(item)
    return result


def load_qa_alignment(path: Path) -> QAAlignmentDocument:
    payload = expect_mapping(read_json_file(path), str(path))
    return QAAlignmentDocument.from_dict(payload)
