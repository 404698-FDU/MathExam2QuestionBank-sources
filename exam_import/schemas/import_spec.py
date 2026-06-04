from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .common import (
    ValidationError,
    expect_bool,
    expect_int,
    expect_mapping,
    expect_optional_string,
    expect_string,
    read_json_file,
)


IMPORT_SPEC_SCHEMA_VERSION = "import_spec_v2"
INPUT_MODES = {"pure_paper", "mixed", "paper_plus_answer", "answer_patch"}
SOURCE_EXTRACTORS = {"local_pymupdf", "mineru_vlm"}
CALL_SPEC_STEPS = {
    "step2_question_ranges",
    "step3_question_json",
    "step35_latex_audit",
    "step4_visual_assets",
    "step4_answer_tables",
}


@dataclass(frozen=True)
class SourceConfig:
    paper_pdf: str = ""
    paper_prepared_dir: str = ""
    paper_page_range: str = ""
    paper_extractor: str = "mineru_vlm"
    answer_pdf: str = ""
    answer_prepared_dir: str = ""
    answer_page_range: str = ""
    answer_extractor: str = "mineru_vlm"
    mixed_pdf: str = ""
    mixed_prepared_dir: str = ""
    mixed_extractor: str = "mineru_vlm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceConfig":
        return cls(
            paper_pdf=expect_optional_string(payload, "paper_pdf"),
            paper_prepared_dir=expect_optional_string(payload, "paper_prepared_dir"),
            paper_page_range=expect_optional_string(payload, "paper_page_range"),
            paper_extractor=_expect_source_extractor(payload, "paper_extractor", default="mineru_vlm"),
            answer_pdf=expect_optional_string(payload, "answer_pdf"),
            answer_prepared_dir=expect_optional_string(payload, "answer_prepared_dir"),
            answer_page_range=expect_optional_string(payload, "answer_page_range"),
            answer_extractor=_expect_source_extractor(payload, "answer_extractor", default="mineru_vlm"),
            mixed_pdf=expect_optional_string(payload, "mixed_pdf"),
            mixed_prepared_dir=expect_optional_string(payload, "mixed_prepared_dir"),
            mixed_extractor=_expect_source_extractor(payload, "mixed_extractor", default="mineru_vlm"),
        )

    def resolve_part_extractor(self, part_name: str) -> str:
        if part_name == "paper":
            return self.paper_extractor
        if part_name == "answer":
            return self.answer_extractor
        if part_name == "mixed":
            return self.mixed_extractor
        raise ValidationError(f"Unsupported source part: {part_name}")


@dataclass(frozen=True)
class MineruConfig:
    token_file: str = ""
    timeout: int = 900
    poll_interval: int = 10
    dpi: int = 144
    extract_retries: int = 3
    extract_retry_sleep: int = 60
    language: str = "ch"
    enable_formula: bool = True
    enable_table: bool = True
    enable_ocr: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MineruConfig":
        return cls(
            token_file=expect_optional_string(payload, "token_file"),
            timeout=expect_int(payload, "timeout", minimum=1, default=900),
            poll_interval=expect_int(payload, "poll_interval", minimum=1, default=10),
            dpi=expect_int(payload, "dpi", minimum=36, default=144),
            extract_retries=expect_int(payload, "extract_retries", minimum=1, default=3),
            extract_retry_sleep=expect_int(payload, "extract_retry_sleep", minimum=0, default=60),
            language=expect_optional_string(payload, "language", default="ch") or "ch",
            enable_formula=expect_bool(payload, "enable_formula", default=True),
            enable_table=expect_bool(payload, "enable_table", default=True),
            enable_ocr=expect_bool(payload, "enable_ocr", default=True),
        )


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    primary_model: str
    timeout: int = 180
    max_workers: int = 8
    enable_thinking: bool = False
    call_spec_path: str = ""
    call_specs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def resolve_call_spec_path(self, step_name: str) -> str:
        if step_name in self.call_specs:
            return self.call_specs[step_name]
        if self.call_spec_path:
            return self.call_spec_path
        raise ValidationError(f"llm.call_specs[{step_name!r}] is required")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LLMConfig":
        call_specs_payload = expect_mapping(payload.get("call_specs", {}), "call_specs")
        call_specs = {str(key): str(value) for key, value in call_specs_payload.items()}
        for step_name, path in call_specs.items():
            if step_name not in CALL_SPEC_STEPS:
                known = ", ".join(sorted(CALL_SPEC_STEPS))
                raise ValidationError(f"Unknown call_specs key: {step_name}. Known steps: {known}")
            if not path:
                raise ValidationError(f"call_specs[{step_name!r}] must be a non-empty string")
        return cls(
            provider=expect_string(payload, "provider"),
            primary_model=expect_string(payload, "primary_model"),
            timeout=expect_int(payload, "timeout", minimum=1, default=180),
            max_workers=expect_int(payload, "max_workers", minimum=1, default=8),
            enable_thinking=expect_bool(payload, "enable_thinking", default=False),
            call_spec_path=expect_optional_string(payload, "call_spec_path"),
            call_specs=call_specs,
        )


@dataclass(frozen=True)
class CachePolicy:
    reuse_existing_ocr: bool = False
    force_source: bool = False
    force_pipeline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CachePolicy":
        return cls(
            reuse_existing_ocr=expect_bool(payload, "reuse_existing_ocr", default=False),
            force_source=expect_bool(payload, "force_source", default=False),
            force_pipeline=expect_bool(payload, "force_pipeline", default=False),
        )


@dataclass(frozen=True)
class StepSwitches:
    skip_step2: bool = False
    skip_step3: bool = False
    step3_5: bool = True
    skip_step4: bool = False
    skip_render: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StepSwitches":
        return cls(
            skip_step2=expect_bool(payload, "skip_step2", default=False),
            skip_step3=expect_bool(payload, "skip_step3", default=False),
            step3_5=expect_bool(payload, "step3_5", default=True),
            skip_step4=expect_bool(payload, "skip_step4", default=False),
            skip_render=expect_bool(payload, "skip_render", default=False),
        )


@dataclass(frozen=True)
class ImportSpec:
    schema_version: str
    run_id: str
    input_mode: str
    runtime_root: str
    runs_root: str
    sources: SourceConfig
    source_rules: dict[str, str] = field(default_factory=dict)
    mineru: MineruConfig = field(default_factory=MineruConfig)
    llm: LLMConfig | None = None
    cache_policy: CachePolicy = field(default_factory=CachePolicy)
    steps: StepSwitches = field(default_factory=StepSwitches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "input_mode": self.input_mode,
            "runtime_root": self.runtime_root,
            "runs_root": self.runs_root,
            "sources": self.sources.to_dict(),
            "source_rules": self.source_rules,
            "mineru": self.mineru.to_dict(),
            "llm": self.llm.to_dict() if self.llm else None,
            "cache_policy": self.cache_policy.to_dict(),
            "steps": self.steps.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ImportSpec":
        schema_version = expect_optional_string(payload, "schema_version", IMPORT_SPEC_SCHEMA_VERSION)
        if schema_version != IMPORT_SPEC_SCHEMA_VERSION:
            raise ValidationError(
                f"schema_version must be {IMPORT_SPEC_SCHEMA_VERSION}, got {schema_version}"
            )
        input_mode = expect_string(payload, "input_mode")
        if input_mode not in INPUT_MODES:
            raise ValidationError(f"input_mode must be one of: {', '.join(sorted(INPUT_MODES))}")
        spec = cls(
            schema_version=schema_version,
            run_id=expect_string(payload, "run_id"),
            input_mode=input_mode,
            runtime_root=expect_optional_string(payload, "runtime_root"),
            runs_root=expect_optional_string(payload, "runs_root"),
            sources=SourceConfig.from_dict(expect_mapping(payload.get("sources"), "sources")),
            source_rules={
                str(key): str(value)
                for key, value in expect_mapping(payload.get("source_rules", {}), "source_rules").items()
            },
            mineru=MineruConfig.from_dict(expect_mapping(payload.get("mineru", {}), "mineru")),
            llm=LLMConfig.from_dict(expect_mapping(payload.get("llm"), "llm")) if payload.get("llm") else None,
            cache_policy=CachePolicy.from_dict(
                expect_mapping(payload.get("cache_policy", {}), "cache_policy")
            ),
            steps=StepSwitches.from_dict(expect_mapping(payload.get("steps", {}), "steps")),
        )
        spec.validate_mode_requirements()
        return spec

    def validate_mode_requirements(self) -> None:
        sources = self.sources
        if self.input_mode == "pure_paper" and not (sources.paper_pdf or sources.paper_prepared_dir):
            raise ValidationError("pure_paper requires sources.paper_pdf or sources.paper_prepared_dir")
        if self.input_mode == "mixed" and not (sources.mixed_pdf or sources.mixed_prepared_dir):
            raise ValidationError("mixed requires sources.mixed_pdf or sources.mixed_prepared_dir")
        if self.input_mode == "paper_plus_answer":
            if not (sources.paper_pdf or sources.paper_prepared_dir):
                raise ValidationError("paper_plus_answer requires paper source input")
            if not (sources.answer_pdf or sources.answer_prepared_dir):
                raise ValidationError("paper_plus_answer requires answer source input")
        if self.input_mode == "answer_patch" and not (sources.answer_pdf or sources.answer_prepared_dir):
            raise ValidationError("answer_patch requires sources.answer_pdf or sources.answer_prepared_dir")


def load_import_spec(path: Path) -> ImportSpec:
    payload = expect_mapping(read_json_file(path), str(path))
    return ImportSpec.from_dict(payload)


def _expect_source_extractor(payload: Mapping[str, Any], field_name: str, default: str) -> str:
    value = expect_optional_string(payload, field_name, default=default) or default
    if value not in SOURCE_EXTRACTORS:
        known = ", ".join(sorted(SOURCE_EXTRACTORS))
        raise ValidationError(f"{field_name} must be one of: {known}")
    return value
