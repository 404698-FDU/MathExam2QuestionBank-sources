from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from exam_import.core.paths import RuntimePaths
from exam_import.schemas.common import (
    ValidationError,
    expect_bool,
    expect_int,
    expect_mapping,
    expect_string,
    expect_string_list,
    read_json_file,
)


@dataclass(frozen=True)
class ModelConfig:
    name: str
    providers: tuple[str, ...]
    context_window: int
    supports_vision: bool
    supports_tool_calling: bool
    supported_response_modes: tuple[str, ...]
    token_estimator: str
    recommended_tpm: int
    default_enable_thinking: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ModelConfig":
        return cls(
            name=expect_string(payload, "name"),
            providers=tuple(expect_string_list(payload, "providers", allow_empty=False)),
            context_window=expect_int(payload, "context_window", minimum=1),
            supports_vision=expect_bool(payload, "supports_vision"),
            supports_tool_calling=expect_bool(payload, "supports_tool_calling"),
            supported_response_modes=tuple(expect_string_list(payload, "supported_response_modes", allow_empty=False)),
            token_estimator=expect_string(payload, "token_estimator"),
            recommended_tpm=expect_int(payload, "recommended_tpm", minimum=1),
            default_enable_thinking=expect_bool(payload, "default_enable_thinking", default=False),
        )


def model_config_dir() -> Path:
    return RuntimePaths.discover().require_provider_config_root() / "models"


def model_config_paths() -> list[Path]:
    return sorted(model_config_dir().glob("*.json"))


def load_model_configs(config_dir: Path | None = None) -> dict[str, ModelConfig]:
    directory = config_dir or model_config_dir()
    if not directory.exists():
        raise FileNotFoundError(f"Model config directory not found: {directory}")
    models: dict[str, ModelConfig] = {}
    for path in sorted(directory.glob("*.json")):
        payload = expect_mapping(read_json_file(path), str(path))
        config = ModelConfig.from_dict(dict(payload))
        if config.name in models:
            raise ValidationError(f"Duplicate model config name: {config.name}")
        models[config.name] = config
    if not models:
        raise ValidationError(f"No model config files found in: {directory}")
    return models


MODELS: dict[str, ModelConfig] = load_model_configs()


def resolve_model_config(name: str) -> ModelConfig:
    try:
        return MODELS[name]
    except KeyError as exc:
        known = ", ".join(sorted(MODELS))
        raise KeyError(f"Unknown model: {name}. Known models: {known}") from exc


def register_model_config(config: ModelConfig) -> None:
    MODELS[config.name] = config
