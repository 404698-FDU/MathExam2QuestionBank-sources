from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import (
    ValidationError,
    expect_enum,
    expect_float,
    expect_int,
    expect_list,
    expect_mapping,
    expect_string,
    expect_string_list,
    read_json_file,
)


RANGE_RISK_TYPES = {"boundary", "missing", "uncertain"}
RANGE_RISK_SEVERITIES = {"info", "warning", "error"}


@dataclass(frozen=True)
class QuestionRange:
    question_no: int
    start_label: str
    end_label: str
    visual_labels: list[str]
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_no": self.question_no,
            "start_label": self.start_label,
            "end_label": self.end_label,
            "visual_labels": list(self.visual_labels),
            "confidence": self.confidence,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QuestionRange":
        confidence = expect_float(payload, "confidence")
        if confidence < 0.0 or confidence > 1.0:
            raise ValidationError("confidence must be between 0 and 1")
        return cls(
            question_no=expect_int(payload, "question_no", minimum=1),
            start_label=expect_string(payload, "start_label"),
            end_label=expect_string(payload, "end_label"),
            visual_labels=expect_string_list(payload, "visual_labels"),
            confidence=confidence,
            reason=expect_string(payload, "reason"),
        )


@dataclass(frozen=True)
class RangeRisk:
    type: str
    question_no: int
    block_labels: list[str]
    severity: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "question_no": self.question_no,
            "block_labels": list(self.block_labels),
            "severity": self.severity,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RangeRisk":
        question_no = expect_int(payload, "question_no", minimum=0)
        return cls(
            type=expect_enum(payload, "type", RANGE_RISK_TYPES),
            question_no=question_no,
            block_labels=expect_string_list(payload, "block_labels"),
            severity=expect_enum(payload, "severity", RANGE_RISK_SEVERITIES),
            evidence=expect_string(payload, "evidence"),
        )


@dataclass(frozen=True)
class QuestionRangesResult:
    question_ranges: list[QuestionRange]
    noise_blocks: list[str]
    risks: list[RangeRisk]

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_ranges": [item.to_dict() for item in self.question_ranges],
            "noise_blocks": list(self.noise_blocks),
            "risks": [item.to_dict() for item in self.risks],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QuestionRangesResult":
        question_ranges_payload = expect_list(payload, "question_ranges")
        if not question_ranges_payload:
            raise ValidationError("question_ranges must not be empty")
        risks_payload = expect_list(payload, "risks")
        return cls(
            question_ranges=[
                QuestionRange.from_dict(expect_mapping(item, f"question_ranges[{index}]"))
                for index, item in enumerate(question_ranges_payload)
            ],
            noise_blocks=expect_string_list(payload, "noise_blocks"),
            risks=[
                RangeRisk.from_dict(expect_mapping(item, f"risks[{index}]"))
                for index, item in enumerate(risks_payload)
            ],
        )


def load_question_ranges_result(path: Path) -> QuestionRangesResult:
    payload = expect_mapping(read_json_file(path), str(path))
    return QuestionRangesResult.from_dict(payload)
