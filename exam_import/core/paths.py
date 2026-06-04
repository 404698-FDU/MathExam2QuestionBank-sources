from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    runtime_root: Path
    package_root: Path
    prompts_root: Path
    provider_config_root: Path
    call_specs_root: Path
    tool_schemas_root: Path

    @classmethod
    def discover(cls) -> "RuntimePaths":
        runtime_root = Path(__file__).resolve().parents[2]
        package_root = runtime_root / "exam_import"
        prompts_root = runtime_root / "prompts"
        provider_config_root = runtime_root / "provider_config"
        call_specs_root = runtime_root / "call_specs"
        tool_schemas_root = runtime_root / "tool_schemas"
        return cls(
            runtime_root=runtime_root,
            package_root=package_root,
            prompts_root=prompts_root,
            provider_config_root=provider_config_root,
            call_specs_root=call_specs_root,
            tool_schemas_root=tool_schemas_root,
        )

    def locale_root(self, locale: str = "zh_CN") -> Path:
        return self.prompts_root / locale

    def require_locale_root(self, locale: str = "zh_CN") -> Path:
        root = self.locale_root(locale)
        if not root.exists():
            raise FileNotFoundError(f"Prompt locale root not found: {root}")
        return root

    def require_tool_schemas_root(self) -> Path:
        root = self.tool_schemas_root
        if not root.exists():
            raise FileNotFoundError(f"Tool schema root not found: {root}")
        return root

    def require_provider_config_root(self) -> Path:
        root = self.provider_config_root
        if not root.exists():
            raise FileNotFoundError(f"Provider config root not found: {root}")
        return root

    def require_call_specs_root(self) -> Path:
        root = self.call_specs_root
        if not root.exists():
            raise FileNotFoundError(f"Call spec root not found: {root}")
        return root

    def resolve_prompt_path(self, relative_path: str, locale: str = "zh_CN") -> Path:
        base = self.require_locale_root(locale)
        candidate_path = Path(relative_path)
        if candidate_path.is_absolute():
            candidate = candidate_path.resolve()
        elif relative_path.startswith("prompts/"):
            candidate = (self.runtime_root / relative_path).resolve()
        elif relative_path.startswith("zh_CN/"):
            candidate = (self.prompts_root / relative_path).resolve()
        else:
            candidate = (base / relative_path).resolve()
        if not candidate.exists():
            raise FileNotFoundError(f"Prompt file not found: {candidate}")
        return candidate
