from __future__ import annotations

import json
from typing import Any

from exam_import.schemas.common import ValidationError

from .client import LLMResponse


def first_message(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValidationError("LLM response missing choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValidationError("LLM response missing choices[0].message")
    return message


def parse_tool_call_arguments(
    response: LLMResponse,
    *,
    expected_tool_name: str,
) -> dict[str, Any]:
    message = first_message(response.body)
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        raise ValidationError("LLM response has no tool_calls")
    tool_call = tool_calls[0]
    function = tool_call.get("function")
    if not isinstance(function, dict):
        raise ValidationError("LLM tool_call missing function payload")
    actual_name = str(function.get("name") or "")
    if actual_name != expected_tool_name:
        raise ValidationError(f"Unexpected tool name: {actual_name!r}, expected {expected_tool_name!r}")
    raw_arguments = function.get("arguments")
    if not isinstance(raw_arguments, str) or not raw_arguments.strip():
        raise ValidationError("LLM tool_call function.arguments is empty")
    parsed = json.loads(raw_arguments)
    if not isinstance(parsed, dict):
        raise ValidationError("LLM tool_call function.arguments must decode to an object")
    return parsed


def parse_json_content(response: LLMResponse) -> dict[str, Any]:
    message = first_message(response.body)
    raw_content = message.get("content")
    if isinstance(raw_content, list):
        text_parts = [item.get("text", "") for item in raw_content if isinstance(item, dict)]
        raw_text = "".join(text_parts).strip()
    else:
        raw_text = str(raw_content or "").strip()
    if not raw_text:
        raise ValidationError("LLM response content is empty")
    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        raise ValidationError("LLM response content must decode to an object")
    return parsed
