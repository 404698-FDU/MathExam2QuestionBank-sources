from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from exam_import.core.paths import RuntimePaths
from exam_import.prompts.registry import resolve_prompt_name


SECTION_INFO_STRING = {
    "meta": "yaml",
    "system": "text",
    "user": "text",
}


def _normalize_section(value: str) -> str:
    return value.strip().lower().replace("_", "-")


@dataclass(frozen=True)
class PromptDocument:
    path: Path
    section: str
    text: str


class PromptLoader:
    def __init__(self, runtime_paths: RuntimePaths | None = None, locale: str = "zh_CN") -> None:
        self.runtime_paths = runtime_paths or RuntimePaths.discover()
        self.locale = locale

    def load(self, prompt_ref: str) -> PromptDocument:
        raw_path, _, section = prompt_ref.partition("#")
        relative_path = resolve_prompt_name(raw_path)
        path = self.runtime_paths.resolve_prompt_path(relative_path, locale=self.locale)
        text = path.read_text(encoding="utf-8")
        normalized_section = _normalize_section(section) if section else ""
        if not normalized_section:
            return PromptDocument(path=path, section="", text=text)
        section_text = self._extract_section(text, normalized_section)
        return PromptDocument(path=path, section=normalized_section, text=section_text)

    def render(self, prompt_ref: str, variables: dict[str, object] | None = None) -> PromptDocument:
        document = self.load(prompt_ref)
        return PromptDocument(
            path=document.path,
            section=document.section,
            text=self._replace_variables(document.text, variables),
        )

    def load_section(self, prompt_ref: str, section: str) -> PromptDocument:
        normalized_section = _normalize_section(section)
        raw_path, _, _ = prompt_ref.partition("#")
        relative_path = resolve_prompt_name(raw_path)
        path = self.runtime_paths.resolve_prompt_path(relative_path, locale=self.locale)
        text = path.read_text(encoding="utf-8")
        section_text = self._extract_section(text, normalized_section)
        return PromptDocument(path=path, section=normalized_section, text=section_text)

    def render_section(
        self,
        prompt_ref: str,
        section: str,
        variables: dict[str, object] | None = None,
    ) -> PromptDocument:
        document = self.load_section(prompt_ref, section)
        return PromptDocument(
            path=document.path,
            section=document.section,
            text=self._replace_variables(document.text, variables),
        )

    def section_text(
        self,
        prompt_ref: str,
        section: str,
        *,
        variables: dict[str, object] | None = None,
        info_string: str | None = None,
    ) -> str:
        normalized_section = _normalize_section(section)
        document = self.render_section(prompt_ref, normalized_section, variables)
        expected_info = info_string or SECTION_INFO_STRING.get(normalized_section)
        return self.first_fenced_block(document.text, info_string=expected_info)

    def load_system_text(self, prompt_ref: str) -> str:
        return self.section_text(prompt_ref, "system")

    def render_user_text(self, prompt_ref: str, variables: dict[str, object] | None = None) -> str:
        return self.section_text(prompt_ref, "user", variables=variables)

    def load_meta(self, prompt_ref: str) -> dict[str, str]:
        meta_text = self.section_text(prompt_ref, "meta", info_string="yaml")
        return self._parse_simple_yaml(meta_text)

    def fenced_blocks(self, text: str, info_string: str | None = None) -> list[str]:
        matches = re.findall(r"```([^\n`]*)\n(.*?)\n```", text, flags=re.S)
        blocks: list[str] = []
        for raw_info, body in matches:
            info = raw_info.strip()
            if info_string is not None and info != info_string:
                continue
            blocks.append(body.strip())
        return blocks

    def first_fenced_block(self, text: str, info_string: str | None = None) -> str:
        blocks = self.fenced_blocks(text, info_string=info_string)
        if not blocks:
            info_label = info_string if info_string is not None else "*"
            raise KeyError(f"No fenced block found for info_string={info_label!r}")
        return blocks[0]

    def _extract_section(self, text: str, section: str) -> str:
        wanted = _normalize_section(section)
        collecting = False
        target_level = 0
        section_lines: list[str] = []
        for line in text.splitlines():
            heading_match = re.match(r"^(#+)\s+(.*)$", line)
            if heading_match:
                level = len(heading_match.group(1))
                heading = _normalize_section(heading_match.group(2))
                if collecting and level <= target_level and heading != wanted:
                    break
                if heading == wanted:
                    collecting = True
                    target_level = level
            if collecting:
                section_lines.append(line)
        if not section_lines:
            raise KeyError(f"Prompt section not found: {section}")
        return "\n".join(section_lines).strip()

    def _replace_variables(self, text: str, variables: dict[str, object] | None) -> str:
        if not variables:
            return text
        rendered = text
        for key, value in variables.items():
            rendered = rendered.replace(f"{{{key}}}", str(value))
        return rendered

    def _parse_simple_yaml(self, text: str) -> dict[str, str]:
        payload: dict[str, str] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                raise ValueError(f"Invalid prompt meta line: {raw_line}")
            key, value = line.split(":", 1)
            payload[key.strip()] = value.strip().strip('"').strip("'")
        return payload
