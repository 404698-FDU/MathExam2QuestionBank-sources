from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
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
OPTION_TAG_RE = re.compile(r'<options no="([^"]+)"\s*/?>')
OPTION_TAG_STRIP_RE = re.compile(r'\s*<options no="[^"]+"\s*/?>\s*')


@dataclass(frozen=True)
class OptionItem:
    label: str
    content_latex: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OptionItem":
        return cls(
            label=expect_string(payload, "label"),
            content_latex=expect_string_list(payload, "content_latex", allow_empty=False),
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
            raise ValidationError("options_latex[].options must be a non-empty array")
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
    stem_latex: list[str]
    options_latex: list[OptionGroup]
    answer_latex: list[str]
    analysis_latex: list[str]
    issues: list[IssueRecord]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "question_no": self.question_no,
            "stem_latex": self.stem_latex,
            "options_latex": [group.to_dict() for group in self.options_latex],
            "answer_latex": self.answer_latex,
            "analysis_latex": self.analysis_latex,
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
        options_payload = payload.get("options_latex")
        if not isinstance(options_payload, list):
            raise ValidationError("options_latex must be an array")
        return cls(
            schema_version=schema_version,
            question_no=question_no,
            stem_latex=expect_string_list(payload, "stem_latex"),
            options_latex=[
                OptionGroup.from_dict(expect_mapping(item, f"options_latex[{index}]"))
                for index, item in enumerate(options_payload)
            ],
            answer_latex=expect_string_list(payload, "answer_latex"),
            analysis_latex=expect_string_list(payload, "analysis_latex"),
            issues=expect_issue_list(payload, "issues"),
        )


def load_question_record(path: Path) -> QuestionRecord:
    payload = expect_mapping(read_json_file(path), str(path))
    return QuestionRecord.from_dict(payload)


def ensure_options_placeholders_in_payload(payload: dict[str, Any]) -> None:
    stem = payload.get("stem_latex")
    options = payload.get("options_latex")
    if not isinstance(stem, list) or not isinstance(options, list) or not options:
        return

    expected_nos: list[str] = []
    for group in options:
        if not isinstance(group, Mapping):
            continue
        value = group.get("no")
        if isinstance(value, str) and value.strip():
            expected_nos.append(value.strip())
    if not expected_nos:
        return

    actual_nos: list[str] = []
    for segment in stem:
        actual_nos.extend(OPTION_TAG_RE.findall(str(segment)))

    if actual_nos == expected_nos and len(actual_nos) == len(set(actual_nos)):
        return

    cleaned_stem: list[str] = []
    for segment in stem:
        cleaned = OPTION_TAG_STRIP_RE.sub(" ", str(segment)).strip()
        if cleaned:
            cleaned_stem.append(cleaned)
    cleaned_stem.extend(f'<options no="{option_no}">' for option_no in expected_nos)
    payload["stem_latex"] = cleaned_stem
