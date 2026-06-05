from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import (
    ValidationError,
    expect_enum,
    expect_float,
    expect_list,
    expect_mapping,
    expect_optional_string,
    expect_string,
    expect_string_list,
    read_json_file,
)

FIELD_NAME_ENUM = {
    "stem_latex",
    "options_latex",
    "answer_latex",
    "analysis_latex",
    "none",
}


@dataclass(frozen=True)
class VisualAssetAssignment:
    label: str
    question_no: int
    placeholder_status: str
    action: str
    source_field: str
    target_field: str
    insert_position: str
    asset_tag: str
    placeholder: str
    option_group_no: str
    option_label: str
    caption_labels: list[str]
    caption_text: str
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VisualAssetAssignment":
        question_no = payload.get("question_no")
        if question_no is not None and not isinstance(question_no, int):
            raise ValidationError("question_no must be an integer or null-equivalent")
        source_field = _expect_field_name(payload, "source_field")
        target_field = _expect_field_name(payload, "target_field")
        return cls(
            label=expect_string(payload, "label"),
            question_no=question_no if isinstance(question_no, int) else 0,
            placeholder_status=expect_enum(
                payload,
                "placeholder_status",
                {
                    "matched",
                    "belongs_but_missing_placeholder",
                    "placeholder_but_not_belong",
                    "wrong_field",
                    "wrong_tag",
                    "no_placeholder_needed",
                    "uncertain",
                },
            ),
            action=expect_enum(
                payload,
                "action",
                {"keep_existing", "add_placeholder", "remove_placeholder", "move_placeholder", "ignore_asset", "review_required"},
            ),
            source_field=source_field,
            target_field=target_field,
            insert_position=expect_enum(
                payload,
                "insert_position",
                {"existing_position", "append_to_field_end", "append_to_option_end", "remove_existing", "none"},
            ),
            asset_tag=expect_enum(payload, "asset_tag", {"img", "table", "chart", "none"}),
            placeholder=expect_optional_string(payload, "placeholder"),
            option_group_no=expect_optional_string(payload, "option_group_no"),
            option_label=expect_optional_string(payload, "option_label"),
            caption_labels=expect_string_list(payload, "caption_labels"),
            caption_text=expect_optional_string(payload, "caption_text"),
            confidence=expect_float(payload, "confidence", default=1.0),
            reason=expect_string(payload, "reason"),
        )


@dataclass(frozen=True)
class VisualAssetRisk:
    label: str
    severity: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VisualAssetRisk":
        return cls(
            label=expect_string(payload, "label"),
            severity=expect_enum(payload, "severity", {"info", "warning", "error"}),
            reason=expect_string(payload, "reason"),
        )


@dataclass(frozen=True)
class VisualAssetReview:
    assets: list[VisualAssetAssignment]
    risks: list[VisualAssetRisk]

    def to_dict(self) -> dict[str, Any]:
        return {
            "assets": [item.to_dict() for item in self.assets],
            "risks": [item.to_dict() for item in self.risks],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VisualAssetReview":
        assets_payload = expect_list(payload, "assets")
        risks_payload = expect_list(payload, "risks")
        return cls(
            assets=[
                VisualAssetAssignment.from_dict(expect_mapping(item, f"assets[{index}]"))
                for index, item in enumerate(assets_payload)
            ],
            risks=[
                VisualAssetRisk.from_dict(expect_mapping(item, f"risks[{index}]"))
                for index, item in enumerate(risks_payload)
            ],
        )


def load_visual_asset_review(path: Path) -> VisualAssetReview:
    payload = expect_mapping(read_json_file(path), str(path))
    return VisualAssetReview.from_dict(payload)


def _expect_field_name(payload: Mapping[str, Any], field_name: str) -> str:
    value = expect_string(payload, field_name)
    if value not in FIELD_NAME_ENUM:
        known = ", ".join(sorted(FIELD_NAME_ENUM))
        raise ValidationError(f"{field_name} must be one of: {known}")
    return value
