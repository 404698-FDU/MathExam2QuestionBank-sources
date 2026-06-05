from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from exam_import.core.io import write_json
from exam_import.schemas.import_spec import INPUT_MODES, SOURCE_EXTRACTORS, ImportSpec


DEFAULT_TEMPLATE = RUNTIME_ROOT / "skills" / "exam-import" / "assets" / "import_spec.standard.template.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a v12 import spec from the standard template.")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="Template JSON path.")
    parser.add_argument("--out", default="", help="Output spec path. Defaults to <runs_root>/specs/<run_id>.json.")
    parser.add_argument("--source-only", action="store_true", help="Drop llm config for source_import-only runs.")

    parser.add_argument("--run-id", required=True, help="Stable ASCII run id.")
    parser.add_argument("--input-mode", required=True, choices=sorted(INPUT_MODES))
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--runs-root", default=None)

    parser.add_argument("--paper-pdf", default=None)
    parser.add_argument("--paper-prepared-dir", default=None)
    parser.add_argument("--paper-page-range", default=None)
    parser.add_argument("--paper-extractor", choices=sorted(SOURCE_EXTRACTORS), default=None)
    parser.add_argument("--answer-pdf", default=None)
    parser.add_argument("--answer-prepared-dir", default=None)
    parser.add_argument("--answer-page-range", default=None)
    parser.add_argument("--answer-extractor", choices=sorted(SOURCE_EXTRACTORS), default=None)
    parser.add_argument("--mixed-pdf", default=None)
    parser.add_argument("--mixed-prepared-dir", default=None)
    parser.add_argument("--mixed-extractor", choices=sorted(SOURCE_EXTRACTORS), default=None)

    parser.add_argument("--paper-source-rule", default=None)
    parser.add_argument("--answer-source-rule", default=None)
    parser.add_argument("--mixed-source-rule", default=None)

    parser.add_argument("--mineru-token-file", default=None)
    parser.add_argument("--mineru-timeout", type=int, default=None)
    parser.add_argument("--mineru-poll-interval", type=int, default=None)
    parser.add_argument("--mineru-dpi", type=int, default=None)
    parser.add_argument("--extract-retries", type=int, default=None)
    parser.add_argument("--extract-retry-sleep", type=int, default=None)

    parser.add_argument("--provider", default=None)
    parser.add_argument("--primary-model", default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--enable-thinking", action="store_true")

    parser.add_argument("--reuse-existing-ocr", action="store_true")
    parser.add_argument("--force-source", action="store_true")
    parser.add_argument("--force-pipeline", action="store_true")
    parser.add_argument("--skip-step2", action="store_true")
    parser.add_argument("--skip-step3", action="store_true")
    parser.add_argument("--no-step3-5", action="store_true")
    parser.add_argument("--skip-step4", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = generate_spec(args)
    spec = ImportSpec.from_dict(payload)
    out_path = resolve_out_path(args.out, spec)
    output_payload = spec.to_dict()
    if args.source_only:
        output_payload.pop("llm", None)
    write_json(out_path, output_payload)
    summary = {
        "spec": str(out_path),
        "source_only": args.source_only,
        "validate_command": f"python.exe exam_import/cli/validate_spec.py --spec {out_path}",
        "source_import_command": f"python.exe exam_import/cli/source_import.py --spec {out_path}",
    }
    if not args.source_only:
        summary["pipeline_command"] = f"python.exe exam_import/cli/run_pipeline.py --spec {out_path}"
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def generate_spec(args: argparse.Namespace) -> dict[str, Any]:
    payload = load_template(Path(args.template))
    payload["run_id"] = args.run_id
    payload["input_mode"] = args.input_mode
    set_if_not_none(payload, "runtime_root", args.runtime_root)
    set_if_not_none(payload, "runs_root", args.runs_root)

    sources = require_object(payload, "sources")
    replace_fields(
        sources,
        {
            "paper_pdf": args.paper_pdf,
            "paper_prepared_dir": args.paper_prepared_dir,
            "paper_page_range": args.paper_page_range,
            "paper_extractor": args.paper_extractor,
            "answer_pdf": args.answer_pdf,
            "answer_prepared_dir": args.answer_prepared_dir,
            "answer_page_range": args.answer_page_range,
            "answer_extractor": args.answer_extractor,
            "mixed_pdf": args.mixed_pdf,
            "mixed_prepared_dir": args.mixed_prepared_dir,
            "mixed_extractor": args.mixed_extractor,
        },
    )

    source_rules = require_object(payload, "source_rules")
    replace_fields(
        source_rules,
        {
            "paper": args.paper_source_rule,
            "answer": args.answer_source_rule,
            "mixed": args.mixed_source_rule,
        },
    )

    mineru = require_object(payload, "mineru")
    replace_fields(
        mineru,
        {
            "token_file": args.mineru_token_file,
            "timeout": args.mineru_timeout,
            "poll_interval": args.mineru_poll_interval,
            "dpi": args.mineru_dpi,
            "extract_retries": args.extract_retries,
            "extract_retry_sleep": args.extract_retry_sleep,
        },
    )

    if args.source_only:
        payload.pop("llm", None)
    elif "llm" in payload:
        llm = require_object(payload, "llm")
        replace_fields(
            llm,
            {
                "provider": args.provider,
                "primary_model": args.primary_model,
                "timeout": args.timeout,
                "max_workers": args.max_workers,
            },
        )
        if args.enable_thinking:
            llm["enable_thinking"] = True

    cache_policy = require_object(payload, "cache_policy")
    if args.reuse_existing_ocr:
        cache_policy["reuse_existing_ocr"] = True
    if args.force_source:
        cache_policy["force_source"] = True
    if args.force_pipeline:
        cache_policy["force_pipeline"] = True

    steps = require_object(payload, "steps")
    if args.skip_step2:
        steps["skip_step2"] = True
    if args.skip_step3:
        steps["skip_step3"] = True
    if args.no_step3_5:
        steps["step3_5"] = False
    if args.skip_step4:
        steps["skip_step4"] = True
    if args.skip_render:
        steps["skip_render"] = True

    return payload


def load_template(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"template must be a JSON object: {path}")
    return payload


def require_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"template field must be an object: {key}")
    return value


def set_if_not_none(payload: dict[str, Any], key: str, value: Any | None) -> None:
    if value is not None:
        payload[key] = value


def replace_fields(payload: dict[str, Any], replacements: dict[str, Any | None]) -> None:
    for key, value in replacements.items():
        if value is not None:
            payload[key] = value


def resolve_out_path(value: str, spec: ImportSpec) -> Path:
    if value:
        return Path(value).resolve()
    runs_root = Path(spec.runs_root)
    if not runs_root.is_absolute():
        runs_root = RUNTIME_ROOT / runs_root
    return (runs_root / "specs" / f"{spec.run_id}.json").resolve()


if __name__ == "__main__":
    raise SystemExit(main())
