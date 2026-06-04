from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os

from exam_import.core.paths import RuntimePaths
from exam_import.schemas.common import (
    ValidationError,
    expect_bool,
    expect_mapping,
    expect_optional_string,
    expect_string,
    expect_string_list,
    read_json_file,
)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    chat_path: str
    token_env_vars: tuple[str, ...]
    token_file_env_vars: tuple[str, ...] = ()
    supports_tool_calling: bool = True
    supports_json_schema: bool = True
    supports_json_object: bool = True
    supports_named_tool_choice: bool = True
    supports_required_tool_choice: bool = False
    named_tool_choice_requires_thinking_disabled: bool = False
    thinking_field: str = "enable_thinking"
    request_headers: dict[str, str] = field(default_factory=dict)

    @property
    def chat_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.chat_path.lstrip('/')}"

    def resolve_token(self, env: dict[str, str] | None = None) -> str:
        env_map = env or os.environ
        for key in self.token_env_vars:
            value = env_map.get(key, "").strip()
            if value:
                return value
        for key in self.token_file_env_vars:
            path_value = env_map.get(key, "").strip()
            if not path_value:
                continue
            token_path = Path(path_value)
            if token_path.exists():
                token = token_path.read_text(encoding="utf-8").strip()
                if token:
                    return token
        expected = ", ".join(self.token_env_vars + self.token_file_env_vars)
        raise RuntimeError(f"Missing API token for provider {self.name}. Expected one of: {expected}")

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ProviderConfig":
        return cls(
            name=expect_string(payload, "name"),
            base_url=expect_string(payload, "base_url"),
            chat_path=expect_string(payload, "chat_path"),
            token_env_vars=tuple(expect_string_list(payload, "token_env_vars", allow_empty=False)),
            token_file_env_vars=tuple(expect_string_list(payload, "token_file_env_vars")),
            supports_tool_calling=expect_bool(payload, "supports_tool_calling", default=True),
            supports_json_schema=expect_bool(payload, "supports_json_schema", default=True),
            supports_json_object=expect_bool(payload, "supports_json_object", default=True),
            supports_named_tool_choice=expect_bool(payload, "supports_named_tool_choice", default=True),
            supports_required_tool_choice=expect_bool(payload, "supports_required_tool_choice", default=False),
            named_tool_choice_requires_thinking_disabled=expect_bool(
                payload,
                "named_tool_choice_requires_thinking_disabled",
                default=False,
            ),
            thinking_field=expect_optional_string(payload, "thinking_field", default="enable_thinking"),
            request_headers=_expect_string_mapping(payload, "request_headers"),
        )


def _expect_string_mapping(payload: dict[str, object], field_name: str) -> dict[str, str]:
    raw = payload.get(field_name, {})
    if raw is None:
        return {}
    mapping = expect_mapping(raw, field_name)
    result: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            raise ValidationError(f"{field_name} keys must be non-empty strings")
        if not isinstance(value, str):
            raise ValidationError(f"{field_name}.{key} must be a string")
        result[key] = value
    return result


def provider_config_dir() -> Path:
    return RuntimePaths.discover().require_provider_config_root() / "providers"


def provider_config_paths() -> list[Path]:
    return sorted(provider_config_dir().glob("*.json"))


def load_provider_configs(config_dir: Path | None = None) -> dict[str, ProviderConfig]:
    directory = config_dir or provider_config_dir()
    if not directory.exists():
        raise FileNotFoundError(f"Provider config directory not found: {directory}")
    providers: dict[str, ProviderConfig] = {}
    for path in sorted(directory.glob("*.json")):
        payload = expect_mapping(read_json_file(path), str(path))
        config = ProviderConfig.from_dict(dict(payload))
        if config.name in providers:
            raise ValidationError(f"Duplicate provider config name: {config.name}")
        providers[config.name] = config
    if not providers:
        raise ValidationError(f"No provider config files found in: {directory}")
    return providers


PROVIDERS: dict[str, ProviderConfig] = load_provider_configs()


def resolve_provider_config(name: str) -> ProviderConfig:
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(PROVIDERS))
        raise KeyError(f"Unknown provider: {name}. Known providers: {known}") from exc


def register_provider_config(config: ProviderConfig) -> None:
    PROVIDERS[config.name] = config
