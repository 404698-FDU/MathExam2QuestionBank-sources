from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from exam_import.core.evidence import EvidenceReport, StepEvidence
from exam_import.core.io import read_json, write_json
from exam_import.core.pipeline_state import StepName
from exam_import.core.run_context import RunContext
from exam_import.prompts.loader import PromptLoader
from exam_import.schemas.import_spec import ImportSpec, load_import_spec
from exam_import.schemas.pipeline_summary import PipelineSummary, load_pipeline_summary
from exam_import.schemas.qa_alignment import QAAlignmentDocument, load_qa_alignment
from exam_import.schemas.question_record import QuestionRecord
from exam_import.steps.step2_runtime import run_step2
from exam_import.steps.step35_normalize import audit_record, call_step35_record, write_step35_outputs
from exam_import.steps.step3_question_json import (
    Step3Job,
    call_step3_job,
    sanitize_step3_asset_placeholders,
    write_step3_outputs,
)
from exam_import.steps.step4_runtime import run_step4
from exam_import.steps.step5_render import render_question_bank


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="v12 pipeline entrypoint.")
    parser.add_argument("--spec", required=True, help="Path to v12 import spec.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = load_import_spec(Path(args.spec))
    summary = run_pipeline(spec)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def run_pipeline(spec: ImportSpec) -> dict[str, Any]:
    run_context = _build_run_context(spec)
    prompt_loader = PromptLoader()
    evidence_steps: list[StepEvidence] = []
    current_records: list[QuestionRecord] | None = None
    qa_alignment: QAAlignmentDocument | None = None
    pipeline_summary: PipelineSummary | None = None

    if not spec.steps.skip_step2:
        llm = _require_llm(spec, "step2_question_ranges")
        step2_summary = run_step2(
            run_context=run_context,
            call_spec_path=Path(llm.resolve_call_spec_path("step2_question_ranges")).resolve(),
            force=spec.cache_policy.force_pipeline,
            prompt_loader=prompt_loader,
        )
        qa_alignment = step2_summary.qa_alignment
        pipeline_summary = step2_summary.pipeline_summary
        evidence_steps.append(
            StepEvidence(
                step=StepName.STEP2,
                metrics=step2_summary.to_metrics(),
                artifacts={
                    "qa_alignment": str(run_context.step2_run_dir / "qa_alignment.json"),
                    "pipeline_summary": str(run_context.step2_run_dir / "pipeline_summary.json"),
                    "crops_manifest": str(run_context.step2_run_dir / "crops_manifest.json"),
                },
            )
        )
    else:
        qa_alignment = _load_optional_qa_alignment(run_context)
        pipeline_summary = _load_optional_pipeline_summary(run_context)

    if not spec.steps.skip_step3:
        qa_alignment = qa_alignment or _load_required_qa_alignment(run_context)
        current_records, step3_summary = _run_step3(
            spec=spec,
            run_context=run_context,
            qa_alignment=qa_alignment,
            prompt_loader=prompt_loader,
        )
        evidence_steps.append(
            StepEvidence(
                step=StepName.STEP3,
                metrics=step3_summary,
                artifacts={"question_bank": str(run_context.question_bank_dir / "question_bank.json")},
            )
        )
    else:
        current_records = _load_question_bank_records(run_context.question_bank_dir / "question_bank.json")

    if spec.steps.step3_5:
        current_records, step35_summary = _run_step35(
            spec=spec,
            run_context=run_context,
            records=current_records,
            prompt_loader=prompt_loader,
        )
        evidence_steps.append(
            StepEvidence(
                step=StepName.STEP35,
                metrics=step35_summary,
                artifacts={"review_dir": str(run_context.review_dir)},
            )
        )

    if not spec.steps.skip_step4:
        current_records, step4_summary = _run_step4(
            spec=spec,
            run_context=run_context,
            qa_alignment=qa_alignment,
            records=current_records,
            prompt_loader=prompt_loader,
        )
        evidence_steps.append(
            StepEvidence(
                step=StepName.STEP4,
                metrics=step4_summary,
                artifacts={
                    "visual_asset_assignment": str(run_context.step2_run_dir / "visual_asset_assignment.json"),
                    "question_bank": str(run_context.question_bank_dir / "question_bank.json"),
                },
            )
        )

    render_summary: dict[str, Any] | None = None
    if not spec.steps.skip_render:
        if current_records is None:
            current_records = _load_question_bank_records(run_context.question_bank_dir / "question_bank.json")
        render_summary = render_question_bank(
            run_context=run_context,
            records=current_records,
            pipeline_summary=pipeline_summary,
        )
        asset_export = render_summary.get("asset_export") if isinstance(render_summary, dict) else {}
        evidence_steps.append(
            StepEvidence(
                step=StepName.STEP5,
                metrics={
                    "question_count": len(current_records),
                    "asset_reference_count": int((asset_export or {}).get("asset_reference_count") or 0),
                    "exported_asset_count": int((asset_export or {}).get("exported_asset_count") or 0),
                    "missing_asset_count": int((asset_export or {}).get("missing_asset_count") or 0),
                },
                artifacts={
                    "render_index": str(run_context.render_dir / "index.html"),
                    "assets_manifest": str(run_context.render_dir / "assets_manifest.json"),
                },
            )
        )

    report = EvidenceReport(
        run_id=run_context.run_id,
        alignment_mode=run_context.alignment_mode,
        steps=evidence_steps,
    )
    reports_dir = Path(spec.runs_root).resolve() / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{run_context.run_id}_v12_pipeline_evidence.json"
    write_json(report_path, report.to_dict())
    return {
        "run_id": run_context.run_id,
        "alignment_mode": run_context.alignment_mode,
        "evidence_report": str(report_path),
        "render": render_summary,
    }


def _build_run_context(spec: ImportSpec) -> RunContext:
    runs_root = Path(spec.runs_root).resolve()
    return RunContext(
        run_id=spec.run_id,
        alignment_mode=spec.input_mode,
        source_runs_root=runs_root / "source_runs",
        step2_root=runs_root / "step2_exam_blocks",
        question_bank_root=runs_root / "question_bank",
        review_root=runs_root / "reviews_step3_5_latex_audit",
        render_root=runs_root / "rendered_question_bank_mathjax",
    )


def _load_required_qa_alignment(run_context: RunContext) -> QAAlignmentDocument:
    path = run_context.step2_run_dir / "qa_alignment.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing Step2 output: {path}")
    return load_qa_alignment(path)


def _load_optional_qa_alignment(run_context: RunContext) -> QAAlignmentDocument | None:
    path = run_context.step2_run_dir / "qa_alignment.json"
    if not path.exists():
        return None
    return load_qa_alignment(path)


def _load_optional_pipeline_summary(run_context: RunContext) -> PipelineSummary | None:
    path = run_context.step2_run_dir / "pipeline_summary.json"
    if not path.exists():
        return None
    return load_pipeline_summary(path)


def _run_step3(
    *,
    spec: ImportSpec,
    run_context: RunContext,
    qa_alignment: QAAlignmentDocument,
    prompt_loader: PromptLoader,
) -> tuple[list[QuestionRecord], dict[str, Any]]:
    llm = _require_llm(spec, "step3_question_json")
    call_spec_path = Path(llm.resolve_call_spec_path("step3_question_json")).resolve()
    records: list[QuestionRecord] = []
    errors: list[dict[str, Any]] = []
    for row in qa_alignment.qa_alignment:
        question_images = _find_step3_images(run_context, row.question_no, "question")
        if not question_images:
            raise FileNotFoundError(f"Missing Step3 question crops for q{row.question_no}: {run_context.question_bank_dir / 'per_question' / 'step3_question_crops'}")
        answer_images = _find_step3_images(run_context, row.question_no, "answer")
        try:
            record = call_step3_job(
                job=Step3Job(
                    question_no=row.question_no,
                    question_image_paths=question_images,
                    answer_image_paths=answer_images,
                ),
                call_spec_path=call_spec_path,
                prompt_loader=prompt_loader,
            )
        except Exception as exc:
            errors.append({"question_no": row.question_no, "error": str(exc)})
            continue
        record = sanitize_step3_asset_placeholders(record=record, qa_alignment=qa_alignment)
        records.append(record)
    if not records:
        raise RuntimeError("Step3 produced no valid question records")
    summary = write_step3_outputs(
        run_context=run_context,
        records=records,
        model_name=llm.primary_model,
        errors=errors,
    )
    return records, summary


def _run_step35(
    *,
    spec: ImportSpec,
    run_context: RunContext,
    records: list[QuestionRecord] | None,
    prompt_loader: PromptLoader,
) -> tuple[list[QuestionRecord], dict[str, Any]]:
    llm = _require_llm(spec, "step35_latex_audit")
    call_spec_path = Path(llm.resolve_call_spec_path("step35_latex_audit")).resolve()
    source_records = records or _load_question_bank_records(run_context.question_bank_dir / "question_bank.json")
    normalized_records: list[QuestionRecord] = []
    before_findings: dict[int, list[Any]] = {}
    after_findings: dict[int, list[Any]] = {}
    for record in source_records:
        before = audit_record(record)
        before_findings[record.question_no] = before
        normalized = call_step35_record(
            record=record,
            call_spec_path=call_spec_path,
            audit_findings=before,
            prompt_loader=prompt_loader,
        )
        normalized_records.append(normalized)
        after_findings[normalized.question_no] = audit_record(normalized)
    summary = write_step35_outputs(
        run_context=run_context,
        normalized_records=normalized_records,
        before_findings=before_findings,
        after_findings=after_findings,
        write_back=True,
    )
    return normalized_records, summary


def _run_step4(
    *,
    spec: ImportSpec,
    run_context: RunContext,
    qa_alignment: QAAlignmentDocument | None,
    records: list[QuestionRecord] | None,
    prompt_loader: PromptLoader,
) -> tuple[list[QuestionRecord], dict[str, Any]]:
    llm = _require_llm(spec, "step4_visual_assets")
    source_records = records or _load_question_bank_records(run_context.question_bank_dir / "question_bank.json")
    step4_result = run_step4(
        run_context=run_context,
        qa_alignment=qa_alignment or _load_required_qa_alignment(run_context),
        records=source_records,
        visual_call_spec_path=Path(llm.resolve_call_spec_path("step4_visual_assets")).resolve(),
        answer_table_call_spec_path=Path(llm.resolve_call_spec_path("step4_answer_tables")).resolve()
        if "step4_answer_tables" in llm.call_specs
        else None,
        prompt_loader=prompt_loader,
    )
    return step4_result.merged_records, step4_result.summary


def _require_llm(spec: ImportSpec, step_name: str):
    if spec.llm is None:
        raise RuntimeError(f"LLM config is required for {step_name}")
    if step_name not in spec.llm.call_specs and not spec.llm.call_spec_path:
        raise RuntimeError(f"Missing llm.call_specs[{step_name!r}] in import spec")
    return spec.llm


def _load_question_bank_records(path: Path) -> list[QuestionRecord]:
    payload = read_json(path)
    if not isinstance(payload, list):
        raise TypeError(f"Question bank must be a JSON array: {path}")
    return [QuestionRecord.from_dict(_require_mapping(item)) for item in payload]


def _find_step3_images(run_context: RunContext, question_no: int, role: str) -> list[Path]:
    if role not in {"question", "answer"}:
        raise ValueError(f"Unsupported crop role: {role}")
    role_prefix = f"{role}_surface"
    search_dirs = [
        run_context.question_bank_dir / "per_question" / f"step3_{role}_crops",
        run_context.step2_run_dir / "crops",
    ]
    patterns = [
        f"{run_context.run_id}_q{question_no:02d}_{role_prefix}_*.png",
        f"{run_context.run_id}_q{question_no:03d}_{role_prefix}_*.png",
        f"*q{question_no:02d}_{role_prefix}_*.png",
        f"*q{question_no:03d}_{role_prefix}_*.png",
    ]
    results: list[Path] = []
    seen: set[Path] = set()
    for base_dir in search_dirs:
        if not base_dir.exists():
            continue
        for pattern in patterns:
            for path in sorted(base_dir.glob(pattern)):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                results.append(resolved)
    return results


def _require_mapping(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("Expected a JSON object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
