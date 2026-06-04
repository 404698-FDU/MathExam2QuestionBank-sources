from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

from exam_import.core.paths import RuntimePaths


TOOL_SCHEMA_FILES = {
    "AnswerTableReview": "answer_table_review.schema.json",
    "QuestionRangesResult": "step2_question_ranges.schema.json",
    "QuestionRecord": "question_record.schema.json",
    "Step2QuestionRanges": "step2_question_ranges.schema.json",
    "Step3Record": "question_record.schema.json",
    "VisualAssetReview": "visual_asset_review.schema.json",
    "submit_question_ranges": "step2_question_ranges.schema.json",
    "submit_step3_5_record": "question_record.schema.json",
    "submit_step4_answer_tables": "answer_table_review.schema.json",
    "submit_step4_visual_assets": "visual_asset_review.schema.json",
}


def resolve_tool_schema(name: str) -> dict:
    try:
        filename = TOOL_SCHEMA_FILES[name]
    except KeyError as exc:
        known = ", ".join(sorted(TOOL_SCHEMA_FILES))
        raise KeyError(f"Unknown tool schema: {name}. Known schemas: {known}") from exc
    return copy.deepcopy(_load_schema_file(filename))


def tool_schema_path(name: str) -> Path:
    try:
        filename = TOOL_SCHEMA_FILES[name]
    except KeyError as exc:
        known = ", ".join(sorted(TOOL_SCHEMA_FILES))
        raise KeyError(f"Unknown tool schema: {name}. Known schemas: {known}") from exc
    return _schema_root() / filename


@lru_cache(maxsize=None)
def _load_schema_file(filename: str) -> dict:
    path = _schema_root() / filename
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError(f"Tool schema file must contain a JSON object: {path}")
    return payload


def _schema_root() -> Path:
    return RuntimePaths.discover().require_tool_schemas_root()
