from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

WORKTREE_ROOT = Path(__file__).resolve().parent
CODE_ROOT = WORKTREE_ROOT.parents[1]
PROJECT_ROOT = WORKTREE_ROOT.parents[2]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import step2_layout  # noqa: E402
from batch_import_2017_2026 import (  # noqa: E402
    extract_one_pdf_with_retries,
    safe_clear_run_dir,
    split_pdf_pages,
)
from common_io import read_json, write_json  # noqa: E402
from common_llm import configure_llm_provider_env  # noqa: E402
from common_token_budget import DEFAULT_OUTPUT_RESERVE_TOKENS, configure_token_budget_env  # noqa: E402


DEFAULT_RUNS_ROOT = WORKTREE_ROOT / "runs"
DEFAULT_MODEL = "Qwen/Qwen3.6-27B"


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_model_tpm_limits(items: list[str] | None) -> dict[str, int]:
    limits: dict[str, int] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--model-tpm-limit must be MODEL=LIMIT, got: {item}")
        model, limit_text = item.rsplit("=", 1)
        model = model.strip()
        if not model:
            raise ValueError(f"--model-tpm-limit has empty model: {item}")
        limit = int(limit_text)
        if limit <= 0:
            raise ValueError(f"--model-tpm-limit must be positive: {item}")
        limits[model] = limit
    return limits


def page_range_slug(page_range: str) -> str:
    return page_range.replace(",", "_").replace("-", "_").replace(" ", "")


def answer_pdf_for_extract(args: argparse.Namespace, run_dir: Path) -> Path:
    answer_pdf = Path(args.answer_pdf).resolve()
    if not answer_pdf.exists():
        raise FileNotFoundError(answer_pdf)
    if not args.answer_page_range:
        return answer_pdf
    split_path = run_dir / "_split_sources" / f"answer_pages_{page_range_slug(args.answer_page_range)}.pdf"
    if args.force_answer and split_path.exists():
        split_path.unlink()
    return split_pdf_pages(answer_pdf, args.answer_page_range, split_path)


def update_source_item(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    source_item_path = run_dir / "source_item.json"
    source_item = read_json(source_item_path, {})
    if not isinstance(source_item, dict):
        raise RuntimeError(f"Invalid source_item.json: {source_item_path}")
    previous_mode = str(source_item.get("mode") or "")
    if not (run_dir / "paper" / "ocr_blocks.json").exists():
        raise FileNotFoundError(f"Existing paper OCR is required: {run_dir / 'paper' / 'ocr_blocks.json'}")
    if args.require_pure_paper and previous_mode not in {"", "pure_paper"}:
        raise RuntimeError(f"{args.run_id} source mode is {previous_mode!r}, not pure_paper")

    source_item["mode"] = "paper_plus_answer_file"
    source_item["answer_pdf"] = str(Path(args.answer_pdf).resolve())
    source_item["answer_page_range"] = args.answer_page_range or ""
    source_item["answer_source_rule"] = args.answer_source_rule or "answer patch added by run_answer_patch.py"
    source_item["answer_patch"] = {
        "patched_at": datetime.now().isoformat(timespec="seconds"),
        "previous_mode": previous_mode,
        "answer_pdf": str(Path(args.answer_pdf).resolve()),
        "answer_page_range": args.answer_page_range or "",
        "answer_source_rule": source_item["answer_source_rule"],
    }
    write_json(source_item_path, source_item)
    return source_item


def extract_answer(args: argparse.Namespace, source_runs_root: Path) -> dict[str, Any]:
    run_dir = source_runs_root / args.run_id
    answer_dir = run_dir / "answer"
    if args.skip_extract:
        if not (answer_dir / "ocr_blocks.json").exists():
            raise FileNotFoundError(f"--skip-extract requires existing answer OCR: {answer_dir / 'ocr_blocks.json'}")
        update_source_item(args, run_dir)
        return {"status": "skipped", "answer_dir": str(answer_dir)}

    if answer_dir.exists() and (answer_dir / "ocr_blocks.json").exists() and not args.force_answer:
        update_source_item(args, run_dir)
        return {"status": "reused", "answer_dir": str(answer_dir)}

    if args.force_answer:
        safe_clear_run_dir(answer_dir, source_runs_root)
    elif answer_dir.exists():
        raise RuntimeError(f"answer directory exists but is incomplete; rerun with --force-answer: {answer_dir}")

    update_source_item(args, run_dir)
    pdf = answer_pdf_for_extract(args, run_dir)
    started = time.monotonic()
    extract_args = argparse.Namespace(
        mineru_token_file=args.mineru_token_file,
        mineru_timeout=args.mineru_timeout,
        mineru_poll_interval=args.mineru_poll_interval,
        extract_retries=args.extract_retries,
        extract_retry_sleep=args.extract_retry_sleep,
        dpi=args.dpi,
    )
    result = extract_one_pdf_with_retries(
        pdf,
        answer_dir,
        extract_args,
        f"{args.run_id}_answer_patch",
        source_runs_root,
    )
    elapsed = round(time.monotonic() - started, 3)
    append_jsonl(
        args.runs_root / "reports" / "answer_patch_timing.jsonl",
        {
            "run_id": args.run_id,
            "stage": "answer_extract",
            "status": "ok",
            "elapsed_seconds": elapsed,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "answer_pdf": str(Path(args.answer_pdf).resolve()),
            "answer_page_range": args.answer_page_range or "",
        },
    )
    return {"status": "extracted", "answer_dir": str(answer_dir), "elapsed_seconds": elapsed, "mineru": result}


def rebuild_step2_answer_alignment(args: argparse.Namespace, source_runs_root: Path, runs_root: Path, env: dict[str, str]) -> dict[str, Any]:
    configure_llm_provider_env(os.environ, args.llm_provider)
    configure_llm_provider_env(env, args.llm_provider)

    source_run_dir = source_runs_root / args.run_id
    paper_dir = source_run_dir / "paper"
    answer_dir = source_run_dir / "answer"
    out_run_dir = runs_root / "step2_exam_blocks" / f"{args.run_id}_raw_units"
    question_range_path = out_run_dir / "step2_pure_paper_ranges.json"
    if not question_range_path.exists():
        raise FileNotFoundError(
            f"Existing pure-paper ranges are required for answer patch: {question_range_path}"
        )
    question_ranges = read_json(question_range_path, {})
    if not isinstance(question_ranges, dict) or not question_ranges.get("question_ranges"):
        raise RuntimeError(f"Invalid existing pure-paper ranges: {question_range_path}")

    paper_ocr = read_json(paper_dir / "ocr_blocks.json", {})
    paper_blocks = step2_layout.layout_items_for_doc(paper_ocr, paper_dir)
    if not paper_blocks:
        raise RuntimeError(f"Missing existing paper layout items for {args.run_id}")
    write_json(out_run_dir / "question_layout_items.json", paper_blocks)
    question_packets = step2_layout.build_packets(
        args.run_id, paper_dir, out_run_dir, paper_blocks, "Q", "question_packets"
    )

    answer_ocr = read_json(answer_dir / "ocr_blocks.json", {})
    answer_blocks = step2_layout.layout_items_for_doc(answer_ocr, answer_dir)
    if not answer_blocks:
        raise RuntimeError(f"Missing answer layout items for {args.run_id}")
    write_json(out_run_dir / "answer_layout_items.json", answer_blocks)
    answer_packets = step2_layout.build_packets(
        args.run_id, answer_dir, out_run_dir, answer_blocks, "A", "answer_packets"
    )

    model = args.step2_model or args.model
    answer_ranges = step2_layout.call_range_detector(
        answer_packets,
        run_id=args.run_id,
        stream="answer",
        mode="pure_answer",
        model=model,
        timeout=args.timeout,
        out_path=out_run_dir / "step2_pure_answer_ranges.json",
        force=args.force_answer_ranges,
    )
    alignment = step2_layout.build_alignment(
        run_id=args.run_id,
        pipeline_mode="paper_plus_answer_file",
        question_ranges=question_ranges,
        answer_ranges=answer_ranges,
        mixed_ranges=None,
        question_packets=question_packets,
        answer_packets=answer_packets,
        out_run_dir=out_run_dir,
    )
    summary = {
        "run_id": args.run_id,
        "mode": "paper_plus_answer_file",
        "model": model,
        "worker_models": [model],
        "range_models": {"pure_paper": "cached_existing", "pure_answer": model},
        "question_count": len(alignment.get("qa_alignment") or []),
        "answered_question_count": sum(1 for row in alignment.get("qa_alignment") or [] if row.get("answer_items")),
        "missing_answer_numbers": alignment.get("missing_answer_numbers") or [],
        "answer_tracking": "separate_answer_items",
        "range_missing_question_numbers": {
            "pure_paper": question_ranges.get("missing_question_numbers_within_detected_span"),
            "pure_answer": answer_ranges.get("missing_question_numbers_within_detected_span"),
        },
        "range_elapsed_seconds": {
            "pure_paper": question_ranges.get("llm_elapsed_seconds"),
            "pure_answer": answer_ranges.get("llm_elapsed_seconds"),
        },
        "step2_timeout_seconds": args.timeout,
        "answer_patch": {
            "patched_at": datetime.now().isoformat(timespec="seconds"),
            "answer_pdf": str(Path(args.answer_pdf).resolve()),
            "answer_page_range": args.answer_page_range or "",
        },
    }
    write_json(out_run_dir / "pipeline_summary.json", summary)
    return summary


def pipeline_command(args: argparse.Namespace, env: dict[str, str]) -> list[str]:
    cmd = [
        sys.executable,
        str(WORKTREE_ROOT / "run_pipeline.py"),
        "--source-runs-root",
        str((args.runs_root / "source_runs").resolve()),
        "--runs-root",
        str((args.runs_root / "step2_exam_blocks").resolve()),
        "--qb-root",
        str((args.runs_root / "question_bank").resolve()),
        "--render-root",
        str((args.runs_root / "rendered_question_bank_mathjax").resolve()),
        "--step3-5-root",
        str((args.runs_root / "reviews_step3_5_latex_audit").resolve()),
        "--run-id",
        args.run_id,
        "--model",
        args.model,
        "--llm-provider",
        args.llm_provider,
        "--timeout",
        str(args.timeout),
        "--max-workers",
        str(args.max_workers),
        "--tpm-limit",
        str(args.tpm_limit),
        "--token-estimator-model",
        args.token_estimator_model or args.model,
        "--image-token-mode",
        args.image_token_mode,
        "--tpm-output-reserve",
        str(args.tpm_output_reserve),
        "--mode",
        "paper_plus_answer_file",
        "--skip-step2",
        "--force",
    ]
    for model in args.worker_model:
        cmd.extend(["--worker-model", model])
    for item in args.model_tpm_limit:
        cmd.extend(["--model-tpm-limit", item])
    if args.token_budget_log:
        cmd.extend(["--token-budget-log", str(args.token_budget_log)])
    if args.token_budget_pool:
        cmd.extend(["--token-budget-pool", str(args.token_budget_pool)])
    if args.token_budget_verbose:
        cmd.append("--token-budget-verbose")
    if args.step3_timeout is not None:
        cmd.extend(["--step3-timeout", str(args.step3_timeout)])
    if args.step3_5_timeout is not None:
        cmd.extend(["--step3-5-timeout", str(args.step3_5_timeout)])
    if args.asset_timeout is not None:
        cmd.extend(["--asset-timeout", str(args.asset_timeout)])
    if args.enable_thinking:
        cmd.append("--enable-thinking")
    else:
        cmd.append("--no-enable-thinking")
    if not args.step3_5:
        cmd.append("--no-step3-5")
    if args.skip_step3:
        cmd.append("--skip-step3")
    if args.skip_step4:
        cmd.append("--skip-step4")
    if args.skip_render:
        cmd.append("--skip-render")
    return cmd


def run_pipeline_after_patch(args: argparse.Namespace, env: dict[str, str]) -> None:
    if args.skip_pipeline:
        return
    cmd = pipeline_command(args, env)
    print(json.dumps({"event": "answer_patch_pipeline_start", "cmd": cmd}, ensure_ascii=False), flush=True)
    subprocess.run(cmd, cwd=WORKTREE_ROOT, env=env, check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Patch an existing pure_paper source run with a newly available answer/analysis PDF."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--answer-pdf", type=Path, required=True)
    parser.add_argument("--answer-page-range", default="")
    parser.add_argument("--answer-source-rule", default="")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--step2-model", default=None)
    parser.add_argument("--worker-model", action="append", default=[])
    parser.add_argument("--llm-provider", choices=["siliconflow", "bailian", "env"], default="siliconflow")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--step3-timeout", type=int, default=None)
    parser.add_argument("--step3-5-timeout", type=int, default=None)
    parser.add_argument("--asset-timeout", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--tpm-limit", type=int, default=0)
    parser.add_argument("--token-estimator-model", default=None)
    parser.add_argument("--image-token-mode", choices=["auto", "processor", "formula"], default="auto")
    parser.add_argument("--tpm-output-reserve", type=int, default=DEFAULT_OUTPUT_RESERVE_TOKENS)
    parser.add_argument("--token-budget-log", type=Path, default=None)
    parser.add_argument("--token-budget-pool", type=Path, default=None)
    parser.add_argument("--token-budget-verbose", action="store_true")
    parser.add_argument("--model-tpm-limit", action="append", default=[])
    parser.add_argument("--mineru-token-file", type=Path, default=PROJECT_ROOT / ".mineru_token")
    parser.add_argument("--mineru-timeout", type=int, default=900)
    parser.add_argument("--mineru-poll-interval", type=int, default=10)
    parser.add_argument("--extract-retries", type=int, default=3)
    parser.add_argument("--extract-retry-sleep", type=int, default=60)
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--force-answer", action="store_true")
    parser.add_argument("--force-answer-ranges", action="store_true")
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument("--skip-pipeline", action="store_true")
    parser.add_argument("--skip-step3", action="store_true")
    parser.add_argument("--step3-5", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-step4", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--require-pure-paper", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.runs_root = args.runs_root.resolve()
    source_runs_root = args.runs_root / "source_runs"
    run_dir = source_runs_root / args.run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"source run does not exist: {run_dir}")

    model_tpm_limits = parse_model_tpm_limits(args.model_tpm_limit)
    env = os.environ.copy()
    configure_llm_provider_env(env, args.llm_provider)
    configure_llm_provider_env(os.environ, args.llm_provider)
    configure_token_budget_env(
        env,
        tpm_limit=args.tpm_limit,
        estimator_model=args.token_estimator_model or args.model,
        image_token_mode=args.image_token_mode,
        output_reserve_tokens=args.tpm_output_reserve,
        verbose=args.token_budget_verbose,
        log_path=args.token_budget_log,
        model_tpm_limits=model_tpm_limits,
        pool_path=args.token_budget_pool,
    )
    configure_token_budget_env(
        os.environ,
        tpm_limit=args.tpm_limit,
        estimator_model=args.token_estimator_model or args.model,
        image_token_mode=args.image_token_mode,
        output_reserve_tokens=args.tpm_output_reserve,
        verbose=args.token_budget_verbose,
        log_path=args.token_budget_log,
        model_tpm_limits=model_tpm_limits,
        pool_path=args.token_budget_pool,
    )

    extract_summary = extract_answer(args, source_runs_root)
    step2_summary = rebuild_step2_answer_alignment(args, source_runs_root, args.runs_root, env)
    run_pipeline_after_patch(args, env)
    final_summary = {
        "run_id": args.run_id,
        "mode": "answer_patch_for_existing_pure_paper",
        "answer_extract": extract_summary,
        "step2": step2_summary,
        "pipeline_ran": not args.skip_pipeline,
        "render": str((args.runs_root / "rendered_question_bank_mathjax" / args.run_id / "index.html").resolve()),
    }
    write_json(args.runs_root / "reports" / f"{args.run_id}_answer_patch_summary.json", final_summary)
    print(json.dumps(final_summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
