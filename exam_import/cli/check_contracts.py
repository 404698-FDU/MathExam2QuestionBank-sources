from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from exam_import.core.paths import RuntimePaths
from exam_import.llm.call_spec_loader import load_and_resolve_call_spec
from exam_import.llm.model_configs import MODELS, model_config_paths
from exam_import.llm.providers import PROVIDERS, provider_config_paths
from exam_import.llm.tool_schemas import TOOL_SCHEMA_FILES, resolve_tool_schema, tool_schema_path
from exam_import.prompts.loader import PromptLoader
from exam_import.prompts.registry import KNOWN_PROMPTS


PROMPT_SECTION_CHECKS = {
    "step2_layout": ["meta", "system", "user"],
    "step3_question_json": ["meta", "system", "user"],
    "step35_latex_audit": ["meta", "system", "user"],
    "step35_latex_patch": ["meta", "system", "user"],
    "step4_visual_assets": ["meta", "system", "user"],
    "step4_answer_tables": ["meta", "system", "user"],
}

SCHEMA_REQUIRED_CHECKS = {
    "step2_question_ranges.schema.json": {"question_ranges", "noise_blocks", "risks"},
    "question_record.schema.json": {
        "schema_version",
        "question_no",
        "stem_latex",
        "options_latex",
        "answer_latex",
        "analysis_latex",
        "issues",
    },
    "step35_latex_patch.schema.json": {"schema_version", "question_no", "edits"},
    "visual_asset_review.schema.json": {"assets", "risks"},
    "answer_table_review.schema.json": {"tables", "risks"},
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check v12 prompt/schema/runtime contract references.")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    summary = check_contracts()
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def check_contracts() -> dict[str, Any]:
    runtime_paths = RuntimePaths.discover()
    prompt_loader = PromptLoader(runtime_paths=runtime_paths)
    prompt_results: list[dict[str, Any]] = []
    prompt_doc_results: list[dict[str, Any]] = []
    for prompt_name, relative_path in sorted(KNOWN_PROMPTS.items()):
        document = prompt_loader.load(prompt_name)
        if document.path.suffix != ".md" or not document.path.name.endswith(".prompt.md"):
            raise RuntimeError(f"Prompt registry must only reference *.prompt.md files: {prompt_name} -> {document.path}")
        prompt_results.append(
            {
                "prompt_ref": prompt_name,
                "path": str(document.path),
                "status": "ok",
                "relative_path": relative_path,
            }
        )
        for section_name in PROMPT_SECTION_CHECKS.get(prompt_name, []):
            section = prompt_loader.load_section(prompt_name, section_name)
            prompt_results.append(
                {
                    "prompt_ref": prompt_name,
                    "section": section_name,
                    "status": "ok",
                    "path": str(section.path),
                }
            )
        if prompt_name in PROMPT_SECTION_CHECKS:
            meta = prompt_loader.load_meta(prompt_name)
            if meta.get("prompt_ref") != prompt_name:
                raise RuntimeError(
                    f"Prompt meta prompt_ref mismatch: prompt_ref={prompt_name}, meta={meta.get('prompt_ref')}"
                )
            source_doc = meta.get("source_doc", "")
            if source_doc:
                source_doc_path = (document.path.parent / source_doc).resolve()
                if not source_doc_path.exists():
                    raise RuntimeError(f"Prompt source_doc not found: {prompt_name} -> {source_doc_path}")
                if not _is_relative_to(source_doc_path, runtime_paths.runtime_root):
                    raise RuntimeError(f"Prompt source_doc must stay inside runtime root: {prompt_name} -> {source_doc_path}")
                prompt_doc_results.append(
                    {
                        "prompt_ref": prompt_name,
                        "source_doc": source_doc,
                        "path": str(source_doc_path),
                        "status": "ok",
                    }
                )

    schema_results: list[dict[str, Any]] = []
    checked_files: set[str] = set()
    for alias in sorted(TOOL_SCHEMA_FILES):
        schema = resolve_tool_schema(alias)
        path = tool_schema_path(alias)
        filename = path.name
        checked_files.add(filename)
        _validate_tool_schema(filename, schema)
        schema_results.append(
            {
                "alias": alias,
                "path": str(path),
                "status": "ok",
            }
        )
    missing_files = sorted(set(SCHEMA_REQUIRED_CHECKS) - checked_files)
    if missing_files:
        raise RuntimeError(f"Unchecked tool schema file(s): {missing_files}")
    provider_results = [
        {
            "path": str(path),
            "status": "ok",
        }
        for path in provider_config_paths()
    ]
    if not provider_results:
        raise RuntimeError("No provider config files found")
    model_results = [
        {
            "path": str(path),
            "status": "ok",
        }
        for path in model_config_paths()
    ]
    if not model_results:
        raise RuntimeError("No model config files found")
    for model_name, model in sorted(MODELS.items()):
        unknown_providers = sorted(set(model.providers) - set(PROVIDERS))
        if unknown_providers:
            raise RuntimeError(
                f"Model config {model_name} references unknown providers: {unknown_providers}"
            )
    call_spec_results: list[dict[str, Any]] = []
    for path in sorted(runtime_paths.require_call_specs_root().glob("*.json")):
        resolved = load_and_resolve_call_spec(path, prompt_loader=prompt_loader)
        call_spec_results.append(
            {
                "path": str(path),
                "step": resolved.call_spec.step,
                "provider": resolved.provider.name,
                "model": resolved.model.name,
                "tool_name": resolved.call_spec.tool_name,
                "status": "ok",
            }
        )

    return {
        "prompt_ref_count": len(prompt_results),
        "prompt_source_doc_count": len(prompt_doc_results),
        "provider_config_count": len(provider_results),
        "model_config_count": len(model_results),
        "tool_schema_alias_count": len(schema_results),
        "call_spec_count": len(call_spec_results),
        "prompts": prompt_results,
        "prompt_source_docs": prompt_doc_results,
        "providers": provider_results,
        "models": model_results,
        "tool_schemas": schema_results,
        "call_specs": call_spec_results,
    }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_tool_schema(filename: str, schema: dict[str, Any]) -> None:
    if schema.get("type") != "object":
        raise RuntimeError(f"Tool schema must be an object schema: {filename}")
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict):
        raise RuntimeError(f"Tool schema must define object properties: {filename}")
    if not isinstance(required, list):
        raise RuntimeError(f"Tool schema must define required fields: {filename}")
    expected_required = SCHEMA_REQUIRED_CHECKS.get(filename)
    if expected_required is None:
        return
    actual_required = set(str(item) for item in required)
    if expected_required != actual_required:
        raise RuntimeError(
            f"Required fields mismatch for {filename}: expected={sorted(expected_required)}, actual={sorted(actual_required)}"
        )
    for field_name in expected_required:
        if field_name not in properties:
            raise RuntimeError(f"Tool schema missing property {field_name!r}: {filename}")


if __name__ == "__main__":
    raise SystemExit(main())
