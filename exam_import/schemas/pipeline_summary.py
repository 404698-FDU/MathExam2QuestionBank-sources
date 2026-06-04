from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .common import ValidationError, expect_int, expect_mapping, expect_optional_string, expect_string, read_json_file


@dataclass(frozen=True)
class PipelineSummary:
    run_id: str
    alignment_mode: str
    question_count: int
    answered_question_count: int
    missing_answer_numbers: list[int] = field(default_factory=list)
    extra_answer_numbers: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PipelineSummary":
        return cls(
            run_id=expect_string(payload, "run_id"),
            alignment_mode=expect_string(payload, "alignment_mode"),
            question_count=expect_int(payload, "question_count", minimum=0),
            answered_question_count=expect_int(payload, "answered_question_count", minimum=0),
            missing_answer_numbers=_int_list(payload, "missing_answer_numbers"),
            extra_answer_numbers=_int_list(payload, "extra_answer_numbers"),
        )


def _int_list(payload: Mapping[str, Any], field_name: str) -> list[int]:
    values = payload.get(field_name, [])
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValidationError(f"{field_name} must be an array")
    result: list[int] = []
    for index, item in enumerate(values):
        if not isinstance(item, int) or item < 1:
            raise ValidationError(f"{field_name}[{index}] must be a positive integer")
        result.append(item)
    return result


def load_pipeline_summary(path: Path) -> PipelineSummary:
    payload = expect_mapping(read_json_file(path), str(path))
    return PipelineSummary.from_dict(payload)
