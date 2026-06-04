from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import (
    IssueRecord,
    ValidationError,
    expect_issue_list,
    expect_mapping,
    expect_string,
    expect_string_list,
    read_json_file,
)


QUESTION_RECORD_SCHEMA_VERSION = "image_only_question_standardization_v1"


@dataclass(frozen=True)
class OptionItem:
    label: str
    content_markdown: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OptionItem":
        return cls(
            label=expect_string(payload, "label"),
            content_markdown=expect_string_list(payload, "content_markdown", allow_empty=False),
        )


@dataclass(frozen=True)
class OptionGroup:
    no: str
    options: list[OptionItem]

    def to_dict(self) -> dict[str, Any]:
        return {"no": self.no, "options": [item.to_dict() for item in self.options]}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OptionGroup":
        options_payload = payload.get("options")
        if not isinstance(options_payload, list) or not options_payload:
            raise ValidationError("options_markdown[].options must be a non-empty array")
        return cls(
            no=expect_string(payload, "no"),
            options=[
                OptionItem.from_dict(expect_mapping(item, f"options[{index}]"))
                for index, item in enumerate(options_payload)
            ],
        )


@dataclass(frozen=True)
class QuestionRecord:
    schema_version: str
    question_no: int
    stem_markdown: list[str]
    options_markdown: list[OptionGroup]
    answer_markdown: list[str]
    analysis_markdown: list[str]
    issues: list[IssueRecord]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "question_no": self.question_no,
            "stem_markdown": self.stem_markdown,
            "options_markdown": [group.to_dict() for group in self.options_markdown],
            "answer_markdown": self.answer_markdown,
            "analysis_markdown": self.analysis_markdown,
            "issues": [issue.to_dict() for issue in self.issues],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QuestionRecord":
        schema_version = expect_string(payload, "schema_version")
        if schema_version != QUESTION_RECORD_SCHEMA_VERSION:
            raise ValidationError(
                f"schema_version must be {QUESTION_RECORD_SCHEMA_VERSION}, got {schema_version}"
            )
        question_no = payload.get("question_no")
        if not isinstance(question_no, int) or question_no < 1:
            raise ValidationError("question_no must be a positive integer")
        options_payload = payload.get("options_markdown")
        if not isinstance(options_payload, list):
            raise ValidationError("options_markdown must be an array")
        return cls(
            schema_version=schema_version,
            question_no=question_no,
            stem_markdown=expect_string_list(payload, "stem_markdown"),
            options_markdown=[
                OptionGroup.from_dict(expect_mapping(item, f"options_markdown[{index}]"))
                for index, item in enumerate(options_payload)
            ],
            answer_markdown=expect_string_list(payload, "answer_markdown"),
            analysis_markdown=expect_string_list(payload, "analysis_markdown"),
            issues=expect_issue_list(payload, "issues"),
        )


def load_question_record(path: Path) -> QuestionRecord:
    payload = expect_mapping(read_json_file(path), str(path))
    return QuestionRecord.from_dict(payload)
