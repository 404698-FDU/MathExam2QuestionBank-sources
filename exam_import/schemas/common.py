from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
from typing import Any, Mapping


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class IssueRecord:
    type: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IssueRecord":
        return cls(
            type=expect_string(payload, "type"),
            severity=expect_enum(payload, "severity", {"info", "warning", "error"}),
            message=expect_string(payload, "message"),
        )


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def expect_mapping(payload: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValidationError(f"{field_name} must be an object")
    return payload


def expect_string(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field_name} must be a non-empty string")
    return value


def expect_optional_string(payload: Mapping[str, Any], field_name: str, default: str = "") -> str:
    value = payload.get(field_name, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    return value


def expect_bool(payload: Mapping[str, Any], field_name: str, default: bool | None = None) -> bool:
    if field_name not in payload:
        if default is None:
            raise ValidationError(f"{field_name} is required")
        return default
    value = payload[field_name]
    if not isinstance(value, bool):
        raise ValidationError(f"{field_name} must be a boolean")
    return value


def expect_int(payload: Mapping[str, Any], field_name: str, minimum: int | None = None, default: int | None = None) -> int:
    if field_name not in payload:
        if default is None:
            raise ValidationError(f"{field_name} is required")
        return default
    value = payload[field_name]
    if not isinstance(value, int):
        raise ValidationError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field_name} must be >= {minimum}")
    return value


def expect_float(payload: Mapping[str, Any], field_name: str, default: float | None = None) -> float:
    if field_name not in payload:
        if default is None:
            raise ValidationError(f"{field_name} is required")
        return default
    value = payload[field_name]
    if not isinstance(value, (int, float)):
        raise ValidationError(f"{field_name} must be numeric")
    return float(value)


def expect_enum(payload: Mapping[str, Any], field_name: str, valid: set[str]) -> str:
    value = expect_string(payload, field_name)
    if value not in valid:
        raise ValidationError(f"{field_name} must be one of: {', '.join(sorted(valid))}")
    return value


def expect_list(payload: Mapping[str, Any], field_name: str) -> list[Any]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise ValidationError(f"{field_name} must be an array")
    return value


def expect_string_list(payload: Mapping[str, Any], field_name: str, allow_empty: bool = True) -> list[str]:
    values = expect_list(payload, field_name)
    result: list[str] = []
    for index, item in enumerate(values):
        if not isinstance(item, str) or not item:
            raise ValidationError(f"{field_name}[{index}] must be a non-empty string")
        result.append(item)
    if not allow_empty and not result:
        raise ValidationError(f"{field_name} must not be empty")
    return result


def expect_issue_list(payload: Mapping[str, Any], field_name: str) -> list[IssueRecord]:
    values = expect_list(payload, field_name)
    return [IssueRecord.from_dict(expect_mapping(item, f"{field_name}[{index}]")) for index, item in enumerate(values)]
