from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from exam_import.core.io import write_json
from exam_import.schemas.import_spec import ImportSpec, load_import_spec
from exam_import.sources import (
    MineruExtractConfig,
    SourcePartPlan,
    extract_part_from_pdf,
    looks_like_source_part,
    validate_source_part_dir,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="v12 source import entrypoint.")
    parser.add_argument("--spec", required=True, help="Path to v12 import spec.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = load_import_spec(Path(args.spec))
    summary = run_source_import(spec)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def run_source_import(spec: ImportSpec) -> dict[str, Any]:
    runs_root = Path(spec.runs_root).resolve()
    source_root = runs_root / "source_runs"
    run_dir = source_root / spec.run_id
    reports_dir = runs_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    if spec.input_mode == "answer_patch":
        _prepare_answer_patch_run_dir(run_dir, spec)
    else:
        _prepare_full_run_dir(run_dir, spec)

    mineru_config = MineruExtractConfig(
        token_file=spec.mineru.token_file,
        timeout=spec.mineru.timeout,
        poll_interval=spec.mineru.poll_interval,
        dpi=spec.mineru.dpi,
        extract_retries=spec.mineru.extract_retries,
        extract_retry_sleep=spec.mineru.extract_retry_sleep,
        language=spec.mineru.language,
        enable_formula=spec.mineru.enable_formula,
        enable_table=spec.mineru.enable_table,
        enable_ocr=spec.mineru.enable_ocr,
    )

    part_results: list[dict[str, Any]] = []
    for plan in _part_plan(spec):
        out_dir = run_dir / plan.part
        if out_dir.exists() and spec.cache_policy.reuse_existing_ocr and looks_like_source_part(out_dir):
            part_results.append(
                {
                    "part": plan.part,
                    "status": "reused",
                    "dir": str(out_dir),
                    "extractor": plan.extractor,
                }
            )
            continue
        prepared_dir = _prepared_dir_for_part(spec, plan.part)
        if prepared_dir is not None:
            if out_dir.exists():
                shutil.rmtree(out_dir)
            shutil.copytree(prepared_dir, out_dir)
            validate_source_part_dir(out_dir)
            part_results.append(
                {
                    "part": plan.part,
                    "status": "copied_prepared_dir",
                    "dir": str(out_dir),
                    "prepared_dir": str(prepared_dir),
                    "extractor": plan.extractor,
                }
            )
            continue
        part_results.append(
            extract_part_from_pdf(
                source_runs_root=source_root,
                run_dir=run_dir,
                plan=plan,
                mineru=mineru_config,
                reuse_existing_ocr=spec.cache_policy.reuse_existing_ocr,
                force_source=spec.cache_policy.force_source,
            )
        )

    source_item = {
        "run_id": spec.run_id,
        "input_mode": spec.input_mode,
        "runtime": "v12",
        "sources": spec.sources.to_dict(),
        "source_rules": spec.source_rules,
        "mineru": spec.mineru.to_dict(),
        "parts": part_results,
    }
    write_json(run_dir / "source_item.json", source_item)
    summary = {
        "run_id": spec.run_id,
        "input_mode": spec.input_mode,
        "source_run": str(run_dir),
        "parts": part_results,
    }
    write_json(reports_dir / f"{spec.run_id}_source_import_summary.json", summary)
    return summary


def _part_plan(spec: ImportSpec) -> list[SourcePartPlan]:
    sources = spec.sources
    if spec.input_mode == "pure_paper":
        return [
            SourcePartPlan(
                part="paper",
                pdf_path=_optional_pdf(sources.paper_pdf),
                page_range=sources.paper_page_range,
                extractor=sources.paper_extractor,
            )
        ]
    if spec.input_mode == "mixed":
        return [
            SourcePartPlan(
                part="mixed",
                pdf_path=_optional_pdf(sources.mixed_pdf),
                page_range="",
                extractor=sources.mixed_extractor,
            )
        ]
    if spec.input_mode == "paper_plus_answer":
        return [
            SourcePartPlan(
                part="paper",
                pdf_path=_optional_pdf(sources.paper_pdf),
                page_range=sources.paper_page_range,
                extractor=sources.paper_extractor,
            ),
            SourcePartPlan(
                part="answer",
                pdf_path=_optional_pdf(sources.answer_pdf),
                page_range=sources.answer_page_range,
                extractor=sources.answer_extractor,
            ),
        ]
    if spec.input_mode == "answer_patch":
        return [
            SourcePartPlan(
                part="answer",
                pdf_path=_optional_pdf(sources.answer_pdf),
                page_range=sources.answer_page_range,
                extractor=sources.answer_extractor,
            )
        ]
    raise ValueError(f"Unsupported input_mode: {spec.input_mode}")


def _prepared_dir_for_part(spec: ImportSpec, part_name: str) -> Path | None:
    sources = spec.sources
    if part_name == "paper":
        return _optional_dir(sources.paper_prepared_dir)
    if part_name == "answer":
        return _optional_dir(sources.answer_prepared_dir)
    if part_name == "mixed":
        return _optional_dir(sources.mixed_prepared_dir)
    raise ValueError(f"Unsupported source part: {part_name}")


def _optional_dir(path_text: str) -> Path | None:
    if not path_text:
        return None
    return Path(path_text).resolve()


def _optional_pdf(path_text: str) -> Path | None:
    if not path_text:
        return None
    return Path(path_text).resolve()


def _prepare_full_run_dir(run_dir: Path, spec: ImportSpec) -> None:
    if run_dir.exists():
        if spec.cache_policy.force_source:
            shutil.rmtree(run_dir)
        elif not spec.cache_policy.reuse_existing_ocr:
            raise RuntimeError(
                f"source run already exists: {run_dir}. "
                "Use cache_policy.reuse_existing_ocr=true or cache_policy.force_source=true."
            )
    run_dir.mkdir(parents=True, exist_ok=True)


def _prepare_answer_patch_run_dir(run_dir: Path, spec: ImportSpec) -> None:
    paper_dir = run_dir / "paper"
    if not paper_dir.exists():
        raise RuntimeError(f"answer_patch requires existing paper source run: {paper_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    answer_dir = run_dir / "answer"
    if answer_dir.exists() and spec.cache_policy.force_source:
        shutil.rmtree(answer_dir)
    elif answer_dir.exists() and not spec.cache_policy.reuse_existing_ocr:
        raise RuntimeError(
            f"answer source already exists: {answer_dir}. "
            "Use cache_policy.reuse_existing_ocr=true or cache_policy.force_source=true."
        )


if __name__ == "__main__":
    raise SystemExit(main())
