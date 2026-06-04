from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SingleLine = Annotated[str, Field(pattern=r"^[^\r\n]*$")]


class OptionsLatex(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    A: list[SingleLine]
    B: list[SingleLine]
    C: list[SingleLine]
    D: list[SingleLine]

    @field_validator("A", "B", "C", "D")
    @classmethod
    def no_empty_items(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip():
                raise ValueError("options_latex item is empty")
        return value


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: SingleLine
    severity: Literal["info", "warning", "error"]
    message: SingleLine


class Step3Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["step3_json_schema_v1"]
    question_no: int
    question_type: Literal["fill_blank", "single_choice", "multiple_choice", "solution", "unknown"]
    stem_latex: list[SingleLine]
    options_latex: OptionsLatex
    answer_latex: list[SingleLine]
    analysis_latex: list[SingleLine]
    rubric_latex: list[SingleLine]
    issues: list[Issue]

    @field_validator("stem_latex", "answer_latex", "analysis_latex", "rubric_latex")
    @classmethod
    def no_empty_text_items(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip():
                raise ValueError("array item is empty")
        return value


class StrictStep3Record(Step3Record):
    @model_validator(mode="after")
    def choice_options_consistent(self) -> "StrictStep3Record":
        if self.question_type in {"single_choice", "multiple_choice"}:
            if not any([self.options_latex.A, self.options_latex.B, self.options_latex.C, self.options_latex.D]):
                raise ValueError("choice question has empty options_latex")
        return self


JSON_SCHEMA = Step3Record.model_json_schema()
STRICT_JSON_SCHEMA = StrictStep3Record.model_json_schema()
