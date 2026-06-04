from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


INPUT_MODES = {"pure_paper", "paper_plus_answer_file", "mixed"}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def page_range_slug(page_range: str) -> str:
    return page_range.replace(",", "_").replace("-", "_").replace(" ", "")


def configure_v11_imports(v11_root: Path) -> Any:
    v11_root = v11_root.resolve()
    code_root = v11_root.parents[1]
    project_root = v11_root.parents[2]
    for path in (v11_root, code_root, project_root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    try:
        import batch_import_2017_2026 as batch_import  # type: ignore
    except Exception as exc:  # pragma: no cover - surfaced to operator.
        raise RuntimeError(f"failed to import v11 batch helpers from {v11_root}: {exc}") from exc
    return batch_import


def source_shape(args: argparse.Namespace) -> str:
    if args.mode == "pure_paper":
        return "paper_only"
    if args.mode == "mixed":
        return "single_pdf_interleaved"
    paper = str(Path(args.paper_pdf).resolve()) if args.paper_pdf else ""
    answer = str(Path(args.answer_pdf).resolve()) if args.answer_pdf else ""
    if paper and answer and paper == answer:
        return "single_pdf_question_then_answer"
    return "split_paper_answer"


def build_source_item(args: argparse.Namespace) -> dict[str, Any]:
    paper_pdf = args.paper_pdf
    if args.mode == "mixed":
        paper_pdf = args.mixed_pdf or args.paper_pdf
    return {
        "run_id": args.run_id,
        "mode": args.mode,
        "source_shape": source_shape(args),
        "paper_pdf": str(Path(paper_pdf).resolve()) if paper_pdf else "",
        "answer_pdf": str(Path(args.answer_pdf).resolve()) if args.answer_pdf else "",
        "paper_page_range": args.paper_page_range or "",
        "answer_page_range": args.answer_page_range or "",
        "paper_source_rule": args.paper_source_rule or "",
        "answer_source_rule": args.answer_source_rule or "",
        "warnings": [],
        "errors": [],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "created_by": "exam-import/scripts/v11_source_import.py",
    }


def part_plan(args: argparse.Namespace) -> list[dict[str, str]]:
    if args.mode == "pure_paper":
        return [{"part": "paper", "pdf": args.paper_pdf, "page_range": args.paper_page_range or ""}]
    if args.mode == "mixed":
        return [{"part": "paper", "pdf": args.mixed_pdf or args.paper_pdf, "page_range": args.paper_page_range or ""}]
    return [
        {"part": "paper", "pdf": args.paper_pdf, "page_range": args.paper_page_range or ""},
        {"part": "answer", "pdf": args.answer_pdf, "page_range": args.answer_page_range or ""},
    ]


def pdf_for_part(batch_import: Any, run_dir: Path, part: str, pdf_text: str, page_range: str, force: bool) -> Path:
    pdf = Path(pdf_text).resolve()
    if not pdf.exists():
        raise FileNotFoundError(pdf)
    if not page_range:
        return pdf
    split_path = run_dir / "_split_sources" / f"{part}_pages_{page_range_slug(page_range)}.pdf"
    if force and split_path.exists():
        split_path.unlink()
    return batch_import.split_pdf_pages(pdf, page_range, split_path)


def extract_part(
    batch_import: Any,
    args: argparse.Namespace,
    source_root: Path,
    run_dir: Path,
    part: str,
    pdf_text: str,
    page_range: str,
) -> dict[str, Any]:
    out_dir = run_dir / part
    ocr_path = out_dir / "ocr_blocks.json"
    if ocr_path.exists():
        if args.reuse_existing_ocr:
            return {"part": part, "status": "reused", "dir": str(out_dir)}
        raise RuntimeError(f"{part} OCR already exists; use --reuse-existing-ocr or --force-source: {ocr_path}")
    if out_dir.exists():
        raise RuntimeError(f"{part} directory exists but OCR is incomplete; use --force-source: {out_dir}")

    pdf = pdf_for_part(batch_import, run_dir, part, pdf_text, page_range, args.force_source)
    extract_args = argparse.Namespace(
        mineru_token_file=args.mineru_token_file,
        mineru_timeout=args.mineru_timeout,
        mineru_poll_interval=args.mineru_poll_interval,
        extract_retries=args.extract_retries,
        extract_retry_sleep=args.extract_retry_sleep,
        dpi=args.dpi,
    )
    started = time.monotonic()
    result = batch_import.extract_one_pdf_with_retries(
        pdf,
        out_dir,
        extract_args,
        f"{args.run_id}_{part}",
        source_root,
    )
    elapsed = round(time.monotonic() - started, 3)
    append_jsonl(
        args.runs_root / "source_import_timing.jsonl",
        {
            "run_id": args.run_id,
            "part": part,
            "status": "ok",
            "elapsed_seconds": elapsed,
            "pdf": str(pdf),
            "page_range": page_range,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return {"part": part, "status": "extracted", "dir": str(out_dir), "elapsed_seconds": elapsed, "mineru": result}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generic PDF source importer for the v11 exam pipeline.")
    parser.add_argument("--v11-root", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=sorted(INPUT_MODES), required=True)
    parser.add_argument("--paper-pdf", default="")
    parser.add_argument("--paper-page-range", default="")
    parser.add_argument("--answer-pdf", default="")
    parser.add_argument("--answer-page-range", default="")
    parser.add_argument("--mixed-pdf", default="")
    parser.add_argument("--paper-source-rule", default="")
    parser.add_argument("--answer-source-rule", default="")
    parser.add_argument("--mineru-token-file", type=Path, default=Path(".mineru_token"))
    parser.add_argument("--mineru-timeout", type=int, default=900)
    parser.add_argument("--mineru-poll-interval", type=int, default=10)
    parser.add_argument("--extract-retries", type=int, default=3)
    parser.add_argument("--extract-retry-sleep", type=int, default=60)
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--force-source", action="store_true")
    parser.add_argument("--reuse-existing-ocr", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.v11_root = args.v11_root.resolve()
    args.runs_root = args.runs_root.resolve()
    if args.reuse_existing_ocr and args.force_source:
        raise ValueError("--reuse-existing-ocr and --force-source cannot both be set")
    if not (args.v11_root / "batch_import_2017_2026.py").exists():
        raise FileNotFoundError(f"missing v11 batch helper: {args.v11_root / 'batch_import_2017_2026.py'}")
    if args.mode in {"pure_paper", "paper_plus_answer_file"} and not args.paper_pdf:
        raise ValueError(f"--paper-pdf is required for mode {args.mode}")
    if args.mode == "paper_plus_answer_file" and not args.answer_pdf:
        raise ValueError("--answer-pdf is required for paper_plus_answer_file")
    if args.mode == "mixed" and not (args.mixed_pdf or args.paper_pdf):
        raise ValueError("--mixed-pdf or --paper-pdf is required for mixed mode")

    batch_import = configure_v11_imports(args.v11_root)
    source_root = args.runs_root / "source_runs"
    run_dir = source_root / args.run_id
    if run_dir.exists() and args.force_source:
        batch_import.safe_clear_run_dir(run_dir, source_root)
    elif run_dir.exists() and not args.reuse_existing_ocr:
        raise RuntimeError(f"source run already exists; use --force-source or --reuse-existing-ocr: {run_dir}")

    run_dir.mkdir(parents=True, exist_ok=True)
    source_item = build_source_item(args)
    write_json(run_dir / "source_item.json", source_item)

    part_results = []
    for item in part_plan(args):
        part_results.append(
            extract_part(
                batch_import=batch_import,
                args=args,
                source_root=source_root,
                run_dir=run_dir,
                part=item["part"],
                pdf_text=item["pdf"],
                page_range=item["page_range"],
            )
        )

    summary = {
        "run_id": args.run_id,
        "mode": args.mode,
        "source_run": str(run_dir),
        "source_item": source_item,
        "parts": part_results,
    }
    write_json(args.runs_root / "reports" / f"{args.run_id}_source_import_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
