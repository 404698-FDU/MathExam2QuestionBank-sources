from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import (
    ValidationError,
    expect_bool,
    expect_enum,
    expect_float,
    expect_int,
    expect_mapping,
    expect_optional_string,
    expect_string,
    read_json_file,
)


CALL_SPEC_SCHEMA_VERSION = "call_spec_v1"
STRUCTURED_OUTPUT_MODES = {"tool_calling", "json_schema", "json_object", "none"}


@dataclass(frozen=True)
class CallInputConfig:
    ocr: bool
    images: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CallInputConfig":
        return cls(
            ocr=expect_bool(payload, "ocr"),
            images=expect_bool(payload, "images"),
        )


@dataclass(frozen=True)
class CallSpec:
    schema_version: str
    step: str
    mode: str
    prompt: str
    provider: str
    model: str
    structured_output: str
    tool_name: str
    tool_schema: str
    temperature: float
    top_p: float
    timeout: int
    max_retries: int
    enable_thinking: bool
    input: CallInputConfig

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["input"] = self.input.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CallSpec":
        schema_version = expect_string(payload, "schema_version")
        if schema_version != CALL_SPEC_SCHEMA_VERSION:
            raise ValidationError(
                f"schema_version must be {CALL_SPEC_SCHEMA_VERSION}, got {schema_version}"
            )
        return cls(
            schema_version=schema_version,
            step=expect_string(payload, "step"),
            mode=expect_string(payload, "mode"),
            prompt=expect_string(payload, "prompt"),
            provider=expect_string(payload, "provider"),
            model=expect_string(payload, "model"),
            structured_output=expect_enum(payload, "structured_output", STRUCTURED_OUTPUT_MODES),
            tool_name=expect_optional_string(payload, "tool_name"),
            tool_schema=expect_optional_string(payload, "tool_schema"),
            temperature=expect_float(payload, "temperature", default=0.0),
            top_p=expect_float(payload, "top_p", default=1.0),
            timeout=expect_int(payload, "timeout", minimum=1),
            max_retries=expect_int(payload, "max_retries", minimum=0, default=0),
            enable_thinking=expect_bool(payload, "enable_thinking", default=False),
            input=CallInputConfig.from_dict(expect_mapping(payload.get("input"), "input")),
        )


def load_call_spec(path: Path) -> CallSpec:
    payload = expect_mapping(read_json_file(path), str(path))
    return CallSpec.from_dict(payload)
