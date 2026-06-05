from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from exam_import.core.execution import RetryResult, call_with_retries
from exam_import.core.io import read_json, write_json, write_jsonl
from exam_import.core.question_bank import question_record_payloads, write_question_bank
from exam_import.core.run_context import RunContext
from exam_import.llm.call_spec_loader import load_and_resolve_call_spec
from exam_import.prompts.loader import PromptLoader
from exam_import.schemas.import_spec import ImportSpec, load_import_spec
from exam_import.schemas.question_record import QuestionRecord
from exam_import.steps.step35_normalize import (
    AuditFinding,
    Step35PatchReview,
    audit_record,
    call_step35_patch_record,
    call_step35_record,
)


DEFAULT_PATCH_CALL_SPEC = "call_specs/step35_latex_audit_patch.dashscope.qwen3.7-plus.tool_calling.json"
DEFAULT_FULL_CALL_SPEC = "call_specs/step35_latex_audit.dashscope.qwen3.7-plus.tool_calling.json"
DEFAULT_OUTPUT_ROOT = "runs/reviews_step3_5_latex_audit_standalone"
RETRY_DELAY_SECONDS = 2.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run standalone Step3.5 full or patch LaTeX audit.")
    parser.add_argument("--spec", required=True, help="Import spec path.")
    parser.add_argument("--mode", choices=["full", "patch"], required=True, help="Step3.5 audit mode.")
    parser.add_argument("--input-question-bank", default="", help="Input question_bank JSON path.")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT, help="Standalone review output root.")
    parser.add_argument("--workers", type=int, default=0, help="Worker count override.")
    parser.add_argument("--call-spec", default="", help="Step3.5 call spec override.")
    parser.add_argument("--write-back", action="store_true", help="Write normalized records back to run question bank.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_standalone_step35(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def run_standalone_step35(args: argparse.Namespace) -> dict[str, Any]:
    spec = load_import_spec(_resolve_path(args.spec))
    run_context = _build_run_context(spec)
    input_path = _resolve_input_question_bank(args.input_question_bank, run_context)
    records = _load_question_bank_records(input_path)
    if not records:
        raise RuntimeError(f"No question records found: {input_path}")

    call_spec_path = _resolve_call_spec_path(args.mode, args.call_spec, spec)
    prompt_loader = PromptLoader()
    resolved = load_and_resolve_call_spec(call_spec_path, prompt_loader=prompt_loader)
    if args.mode == "patch" and resolved.call_spec.mode != "patch_normalize":
        raise RuntimeError(f"Patch mode requires patch_normalize call spec, got {resolved.call_spec.mode}")
    if args.mode == "full" and resolved.call_spec.mode != "full_normalize":
        raise RuntimeError(f"Full mode requires full_normalize call spec, got {resolved.call_spec.mode}")

    max_workers = _resolve_worker_count(args.workers, spec, records)
    max_attempts = max(1, resolved.call_spec.max_retries + 1)
    before_findings = {record.question_no: audit_record(record) for record in records}
    started = time.monotonic()
    if args.mode == "full":
        normalized_records, run_metrics, patch_reviews = _run_full_mode(
            records=records,
            call_spec_path=call_spec_path,
            prompt_loader=prompt_loader,
            before_findings=before_findings,
            max_workers=max_workers,
            max_attempts=max_attempts,
        )
    else:
        normalized_records, run_metrics, patch_reviews = _run_patch_mode(
            records=records,
            call_spec_path=call_spec_path,
            prompt_loader=prompt_loader,
            before_findings=before_findings,
            max_workers=max_workers,
            max_attempts=max_attempts,
        )
    elapsed_seconds = round(time.monotonic() - started, 3)
    after_findings = {record.question_no: audit_record(record) for record in normalized_records}
    out_dir = _resolve_output_dir(args.output_root, args.mode, spec.run_id)
    summary = _write_outputs(
        out_dir=out_dir,
        run_context=run_context,
        mode=args.mode,
        input_path=input_path,
        call_spec_path=call_spec_path,
        model_name=resolved.model.name,
        records=normalized_records,
        before_findings=before_findings,
        after_findings=after_findings,
        patch_reviews=patch_reviews,
        elapsed_seconds=elapsed_seconds,
        run_metrics=run_metrics,
        write_back=bool(args.write_back),
    )
    return summary


def _run_full_mode(
    *,
    records: list[QuestionRecord],
    call_spec_path: Path,
    prompt_loader: PromptLoader,
    before_findings: dict[int, list[AuditFinding]],
    max_workers: int,
    max_attempts: int,
) -> tuple[list[QuestionRecord], dict[str, int], list[dict[str, Any]]]:
    normalized_by_qno: dict[int, QuestionRecord] = {}
    failures: list[dict[str, Any]] = []
    attempt_count = 0
    retry_count = 0
    worker_count = max(1, min(max_workers, len(records)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                call_with_retries,
                lambda record=record: call_step35_record(
                    record=record,
                    call_spec_path=call_spec_path,
                    audit_findings=before_findings[record.question_no],
                    prompt_loader=prompt_loader,
                ),
                max_attempts=max_attempts,
                retry_delay_seconds=RETRY_DELAY_SECONDS,
            ): record.question_no
            for record in records
        }
        for future in as_completed(future_map):
            question_no = future_map[future]
            result: RetryResult = future.result()
            attempt_count += result.attempts
            retry_count += max(0, result.attempts - 1)
            if not result.success:
                failures.append(_failure_payload(question_no, result))
                continue
            normalized = result.value
            normalized_by_qno[normalized.question_no] = normalized
    if failures:
        failure = failures[0]
        raise RuntimeError(f"Standalone Step3.5 full failed for q{failure['question_no']}: {failure['error']}")
    return (
        [normalized_by_qno[qno] for qno in sorted(normalized_by_qno)],
        {
            "worker_count": worker_count,
            "attempt_count": attempt_count,
            "retry_count": retry_count,
            "model_call_question_count": len(records),
            "skipped_no_finding_count": 0,
            "patched_question_count": 0,
            "patch_edit_count": 0,
            "error_count": 0,
        },
        [],
    )


def _run_patch_mode(
    *,
    records: list[QuestionRecord],
    call_spec_path: Path,
    prompt_loader: PromptLoader,
    before_findings: dict[int, list[AuditFinding]],
    max_workers: int,
    max_attempts: int,
) -> tuple[list[QuestionRecord], dict[str, int], list[dict[str, Any]]]:
    normalized_by_qno: dict[int, QuestionRecord] = {
        record.question_no: record for record in records if not before_findings[record.question_no]
    }
    jobs = [record for record in records if before_findings[record.question_no]]
    failures: list[dict[str, Any]] = []
    patch_reviews: list[dict[str, Any]] = []
    attempt_count = 0
    retry_count = 0
    worker_count = max(1, min(max_workers, len(jobs) or 1))
    if jobs:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    call_with_retries,
                    lambda record=record: call_step35_patch_record(
                        record=record,
                        call_spec_path=call_spec_path,
                        audit_findings=before_findings[record.question_no],
                        prompt_loader=prompt_loader,
                    ),
                    max_attempts=max_attempts,
                    retry_delay_seconds=RETRY_DELAY_SECONDS,
                ): record.question_no
                for record in jobs
            }
            for future in as_completed(future_map):
                question_no = future_map[future]
                result: RetryResult = future.result()
                attempt_count += result.attempts
                retry_count += max(0, result.attempts - 1)
                if not result.success:
                    failures.append(_failure_payload(question_no, result))
                    continue
                normalized, review = result.value
                normalized_by_qno[normalized.question_no] = normalized
                patch_reviews.append(review.to_dict())
    if failures:
        failure = failures[0]
        raise RuntimeError(f"Standalone Step3.5 patch failed for q{failure['question_no']}: {failure['error']}")
    return (
        [normalized_by_qno[qno] for qno in sorted(normalized_by_qno)],
        {
            "worker_count": worker_count,
            "attempt_count": attempt_count,
            "retry_count": retry_count,
            "model_call_question_count": len(jobs),
            "skipped_no_finding_count": len(records) - len(jobs),
            "patched_question_count": sum(1 for review in patch_reviews if review.get("edits")),
            "patch_edit_count": sum(len(review.get("edits") or []) for review in patch_reviews),
            "error_count": 0,
        },
        sorted(patch_reviews, key=lambda item: int(item["question_no"])),
    )


def _write_outputs(
    *,
    out_dir: Path,
    run_context: RunContext,
    mode: str,
    input_path: Path,
    call_spec_path: Path,
    model_name: str,
    records: list[QuestionRecord],
    before_findings: dict[int, list[AuditFinding]],
    after_findings: dict[int, list[AuditFinding]],
    patch_reviews: list[dict[str, Any]],
    elapsed_seconds: float,
    run_metrics: dict[str, int],
    write_back: bool,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    payloads = question_record_payloads(records)
    write_json(out_dir / "normalized_question_bank.json", payloads)
    write_json(out_dir / "merged_question_bank.json", payloads)
    write_jsonl(out_dir / "merged_question_bank.jsonl", payloads)
    write_json(out_dir / "before_findings.json", _findings_payload(before_findings))
    write_json(out_dir / "after_findings.json", _findings_payload(after_findings))
    if patch_reviews:
        write_json(out_dir / "patch_reviews.json", patch_reviews)
    backup_path = ""
    if write_back:
        backup_path = _backup_and_write_question_bank(run_context, records, mode)
    summary = {
        "run_id": run_context.run_id,
        "mode": mode,
        "model": model_name,
        "input_question_bank": str(input_path),
        "output_dir": str(out_dir),
        "call_spec": str(call_spec_path),
        "question_count": len(payloads),
        "normalized_count": len(payloads),
        "before_finding_count": sum(len(values) for values in before_findings.values()),
        "after_finding_count": sum(len(values) for values in after_findings.values()),
        "elapsed_seconds": elapsed_seconds,
        "write_back": write_back,
        "backup_path": backup_path,
        **run_metrics,
    }
    write_json(out_dir / "summary.json", summary)
    return summary


def _backup_and_write_question_bank(run_context: RunContext, records: list[QuestionRecord], mode: str) -> str:
    qb_dir = run_context.question_bank_dir
    qb_path = qb_dir / "question_bank.json"
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = qb_dir / f"question_bank.before_step35_{mode}_{timestamp}.json"
    if qb_path.exists():
        shutil.copy2(qb_path, backup_path)
    write_question_bank(qb_dir, records)
    return str(backup_path)


def _findings_payload(findings_by_qno: dict[int, list[AuditFinding]]) -> dict[str, list[dict[str, Any]]]:
    return {
        str(question_no): [finding.to_dict() for finding in findings]
        for question_no, findings in sorted(findings_by_qno.items())
    }


def _failure_payload(question_no: int, result: RetryResult) -> dict[str, Any]:
    return {
        "question_no": question_no,
        "error": str(result.error),
        "attempt_count": result.attempts,
        "elapsed_seconds": result.elapsed_seconds,
    }


def _load_question_bank_records(path: Path) -> list[QuestionRecord]:
    payload = read_json(path)
    if not isinstance(payload, list):
        raise RuntimeError(f"Question bank must be a JSON array: {path}")
    return [QuestionRecord.from_dict(item) for item in payload]


def _resolve_input_question_bank(value: str, run_context: RunContext) -> Path:
    if value:
        return _resolve_path(value)
    candidates = [
        run_context.question_bank_dir / "question_bank.before_step3_5_latex_audit.json",
        run_context.question_bank_dir / "question_bank.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(f"No Step3 input question bank found under {run_context.question_bank_dir}")


def _resolve_call_spec_path(mode: str, value: str, spec: ImportSpec) -> Path:
    if value:
        return _resolve_path(value)
    if mode == "full" and spec.llm:
        return _resolve_path(spec.llm.resolve_call_spec_path("step35_latex_audit"))
    default_path = DEFAULT_PATCH_CALL_SPEC if mode == "patch" else DEFAULT_FULL_CALL_SPEC
    return _resolve_path(default_path)


def _resolve_output_dir(output_root: str, mode: str, run_id: str) -> Path:
    return (_resolve_path(output_root) / mode / run_id).resolve()


def _resolve_worker_count(value: int, spec: ImportSpec, records: list[QuestionRecord]) -> int:
    if value > 0:
        return min(value, len(records))
    if spec.llm:
        return min(max(1, spec.llm.max_workers), len(records))
    return min(8, len(records))


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    cwd_candidate = (Path.cwd() / path).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (RUNTIME_ROOT / path).resolve()


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


if __name__ == "__main__":
    raise SystemExit(main())
