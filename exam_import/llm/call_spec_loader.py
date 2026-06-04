from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from exam_import.prompts.loader import PromptDocument, PromptLoader
from exam_import.schemas.call_spec import CallSpec, load_call_spec

from .model_configs import ModelConfig, resolve_model_config
from .providers import ProviderConfig, resolve_provider_config


@dataclass(frozen=True)
class ResolvedCallSpec:
    call_spec: CallSpec
    provider: ProviderConfig
    model: ModelConfig
    prompt: PromptDocument


def load_and_resolve_call_spec(
    path: Path,
    prompt_loader: PromptLoader | None = None,
) -> ResolvedCallSpec:
    call_spec = load_call_spec(path)
    provider = resolve_provider_config(call_spec.provider)
    model = resolve_model_config(call_spec.model)
    if provider.name not in model.providers:
        raise ValueError(
            f"Call spec provider/model mismatch: provider={provider.name}, model={model.name}, expected one of={model.providers}"
        )
    if call_spec.structured_output not in model.supported_response_modes:
        raise ValueError(
            f"Model {model.name} does not support structured_output={call_spec.structured_output}"
        )
    if call_spec.input.images and not model.supports_vision:
        raise ValueError(f"Model {model.name} does not support image input")
    if call_spec.structured_output == "tool_calling" and not provider.supports_tool_calling:
        raise ValueError(f"Provider {provider.name} does not support tool calling")
    if call_spec.structured_output == "json_schema" and not provider.supports_json_schema:
        raise ValueError(f"Provider {provider.name} does not support json_schema")
    if call_spec.structured_output == "json_object" and not provider.supports_json_object:
        raise ValueError(f"Provider {provider.name} does not support json_object")
    loader = prompt_loader or PromptLoader()
    prompt = loader.load(call_spec.prompt)
    return ResolvedCallSpec(call_spec=call_spec, provider=provider, model=model, prompt=prompt)
