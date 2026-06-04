from __future__ import annotations

from typing import Any

from .call_spec_loader import ResolvedCallSpec


def build_request_payload(
    resolved: ResolvedCallSpec,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | None = None,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = resolved.call_spec
    payload: dict[str, Any] = {
        "model": resolved.model.name,
        "messages": messages,
        "temperature": spec.temperature,
        "top_p": spec.top_p,
    }
    if resolved.provider.thinking_field:
        payload[resolved.provider.thinking_field] = spec.enable_thinking
    if spec.structured_output == "tool_calling":
        _validate_tool_choice(resolved, tool_choice)
        payload["tools"] = tools or []
        payload["parallel_tool_calls"] = False
        if tool_choice:
            payload["tool_choice"] = tool_choice
    elif spec.structured_output == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif spec.structured_output == "json_schema":
        if not json_schema:
            raise ValueError("json_schema structured output requires json_schema payload")
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": spec.tool_name or spec.step,
                "strict": True,
                "schema": json_schema,
            },
        }
    return payload


def _validate_tool_choice(resolved: ResolvedCallSpec, tool_choice: dict[str, Any] | None) -> None:
    if not tool_choice:
        return
    if isinstance(tool_choice, str):
        if tool_choice == "required" and not resolved.provider.supports_required_tool_choice:
            raise ValueError(f"Provider {resolved.provider.name} does not support tool_choice='required'")
        return
    if not isinstance(tool_choice, dict):
        raise ValueError(f"Unsupported tool_choice payload: {tool_choice!r}")
    if tool_choice.get("type") != "function":
        return
    if not resolved.provider.supports_named_tool_choice:
        raise ValueError(f"Provider {resolved.provider.name} does not support named tool_choice")
    if (
        resolved.call_spec.enable_thinking
        and resolved.provider.named_tool_choice_requires_thinking_disabled
    ):
        raise ValueError(
            f"Provider {resolved.provider.name} requires enable_thinking=false when forcing a named tool_choice"
        )
