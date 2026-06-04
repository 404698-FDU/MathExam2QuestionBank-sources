from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter


WORKTREE_ROOT = Path(__file__).resolve().parent
CODE_ROOT = WORKTREE_ROOT.parents[1]
PROJECT_ROOT = WORKTREE_ROOT.parents[2]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import exam_agent_pipeline as ep  # noqa: E402


DEFAULT_MODEL = "Qwen/Qwen3.6-35B-A3B"
DEFAULT_RUNS_ROOT = WORKTREE_ROOT / "runs"
GAOKAO_2009_2025 = PROJECT_ROOT / "gaokaomath_shanghai" / "2009_2025"
GAOKAO_PAPERS = PROJECT_ROOT / "gaokao_papers"
SPRING_QUESTION_THEN_ANSWER_RANGES = {
    2017: ("1-3", "4-4"),
    2018: ("1-5", "6-6"),
    2019: ("1-4", "5-5"),
    2020: ("1-3", "4-4"),
}


@dataclass
class ImportItem:
    run_id: str
    year: int
    season: str
    mode: str
    source_shape: str
    paper_pdf: str | None
    answer_pdf: str | None = None
    source_pdf: str | None = None
    paper_page_range: str = ""
    answer_page_range: str = ""
    paper_source_rule: str = ""
    answer_source_rule: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.errors


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def existing(path: Path) -> str | None:
    return str(path) if path.exists() else None


def spring_solution_pdf(year: int, warnings: list[str], errors: list[str]) -> str | None:
    analysis = GAOKAO_2009_2025 / f"{year}年高考数学试卷（上海）（春考）（解析卷）.pdf"
    if analysis.exists():
        return str(analysis)
    answer = GAOKAO_2009_2025 / f"{year}年高考数学试卷（上海）（春考）（答案卷）.pdf"
    if answer.exists():
        warnings.append("required 春考解析卷 not present; using same-root 答案卷 naming variant")
        return str(answer)
    errors.append(f"missing spring analysis PDF under required root: {analysis}")
    candidate = GAOKAO_PAPERS / f"{year}年高考数学试卷（上海）（春考）（答案卷）.pdf"
    if candidate.exists():
        warnings.append(f"candidate exists outside required root but not used automatically: {candidate}")
    return None


def spring_split_pdf(year: int, role: str, root: Path, errors: list[str]) -> str | None:
    role_name = "空白卷" if role == "paper" else "答案卷"
    path = root / f"{year}年高考数学试卷（上海）（春考）（{role_name}）.pdf"
    if path.exists():
        return str(path)
    errors.append(f"missing {year} spring {role_name} PDF: {path}")
    return None


def spring_blank_pdf(year: int, errors: list[str]) -> str | None:
    path = GAOKAO_2009_2025 / f"{year}年高考数学试卷（上海）（春考）（空白卷）.pdf"
    if path.exists():
        return str(path)
    errors.append(f"missing {year} spring 空白卷 PDF: {path}")
    return None


def autumn_answer_pdf(year: int, warnings: list[str], errors: list[str], prefer_answer: bool = False) -> str | None:
    answer = GAOKAO_2009_2025 / f"{year}年高考数学试卷（上海）（秋考）（答案卷）.pdf"
    if prefer_answer:
        if answer.exists():
            return str(answer)
        errors.append(f"missing autumn answer PDF under required root: {answer}")
        return None
    analysis = GAOKAO_2009_2025 / f"{year}年高考数学试卷（上海）（秋考）（解析卷）.pdf"
    if analysis.exists():
        return str(analysis)
    if answer.exists():
        warnings.append("required 秋考解析卷 not present; using same-root 答案卷 naming variant")
        return str(answer)
    errors.append(f"missing autumn analysis PDF under required root: {analysis}")
    return None


def build_import_items() -> list[ImportItem]:
    items: list[ImportItem] = []
    for year, (paper_pages, answer_pages) in SPRING_QUESTION_THEN_ANSWER_RANGES.items():
        warnings: list[str] = []
        errors: list[str] = []
        source_pdf = spring_solution_pdf(year, warnings, errors)
        paper_pdf = source_pdf
        paper_page_range = paper_pages
        paper_source_rule = f"春考解析卷先试题后答案；试题页 {paper_pages}"
        source_shape = "single_pdf_question_then_answer"
        if year == 2020:
            hi_quality_paper = GAOKAO_2009_2025 / "hi_quality" / "2020" / "2020春季上海.pdf"
            if hi_quality_paper.exists():
                paper_pdf = str(hi_quality_paper)
                paper_page_range = ""
                paper_source_rule = r"2020 spring paper from 2009_2025\hi_quality\2020\2020春季上海.pdf"
                source_shape = "split_paper_answer"
            else:
                errors.append(f"missing 2020 spring hi_quality paper PDF: {hi_quality_paper}")
        items.append(
            ImportItem(
                run_id=f"shanghai_{year}_spring_paper_answer",
                year=year,
                season="spring",
                mode="paper_plus_answer_file",
                source_shape=source_shape,
                source_pdf=source_pdf,
                paper_pdf=paper_pdf,
                answer_pdf=source_pdf,
                paper_page_range=paper_page_range,
                answer_page_range=answer_pages,
                paper_source_rule=paper_source_rule,
                answer_source_rule=f"春考解析卷先试题后答案；答案页 {answer_pages}",
                warnings=warnings,
                errors=errors,
            )
        )

    for year in range(2021, 2025):
        warnings = []
        errors = []
        paper_pdf = spring_solution_pdf(year, warnings, errors)
        source_pdf = paper_pdf
        mode = "mixed"
        source_shape = "single_pdf_interleaved"
        answer_pdf = None
        paper_source_rule = r"gaokaomath_shanghai\2009_2025 春考解析卷试题答案混排 direct mixed import"
        answer_source_rule = ""
        if year == 2023:
            hi_quality_paper = GAOKAO_2009_2025 / "hi_quality" / "2023" / "2023春季上海.pdf"
            if hi_quality_paper.exists():
                mode = "paper_plus_answer_file"
                source_shape = "split_paper_answer"
                answer_pdf = paper_pdf
                paper_pdf = str(hi_quality_paper)
                paper_source_rule = r"2023 spring paper from 2009_2025\hi_quality\2023\2023春季上海.pdf"
                answer_source_rule = r"2023 spring answer preserved from existing mixed analysis PDF extraction"
            else:
                errors.append(f"missing 2023 spring hi_quality paper PDF: {hi_quality_paper}")
        items.append(
            ImportItem(
                run_id=f"shanghai_{year}_spring_mixed",
                year=year,
                season="spring",
                mode=mode,
                source_shape=source_shape,
                paper_pdf=paper_pdf,
                answer_pdf=answer_pdf,
                source_pdf=source_pdf,
                paper_source_rule=paper_source_rule,
                answer_source_rule=answer_source_rule,
                warnings=warnings,
                errors=errors,
            )
        )

    for year, root in ((2025, GAOKAO_2009_2025), (2026, GAOKAO_PAPERS)):
        warnings = []
        errors = []
        paper_pdf = spring_split_pdf(year, "paper", root, errors)
        answer_pdf = spring_split_pdf(year, "answer", root, errors)
        items.append(
            ImportItem(
                run_id=f"shanghai_{year}_spring_paper_answer",
                year=year,
                season="spring",
                mode="paper_plus_answer_file",
                source_shape="split_blank_answer",
                paper_pdf=paper_pdf,
                answer_pdf=answer_pdf,
                paper_source_rule=f"{year} spring paper from {root} blank PDF",
                answer_source_rule=f"{year} spring answer from {root} answer PDF",
                warnings=warnings,
                errors=errors,
            )
        )

    for year in range(2017, 2025):
        warnings = []
        errors = []
        paper = GAOKAO_2009_2025 / "hi_quality" / str(year) / f"{year}上海.pdf"
        if not paper.exists():
            errors.append(f"missing autumn hi_quality paper PDF: {paper}")
        answer_pdf = autumn_answer_pdf(year, warnings, errors)
        items.append(
            ImportItem(
                run_id=f"shanghai_{year}_autumn_paper_answer",
                year=year,
                season="autumn",
                mode="paper_plus_answer_file",
                source_shape="split_paper_answer",
                paper_pdf=existing(paper),
                answer_pdf=answer_pdf,
                paper_source_rule=r"2017-2024 autumn paper from 2009_2025\hi_quality",
                answer_source_rule=r"autumn answer from 2009_2025 analysis PDF",
                warnings=warnings,
                errors=errors,
            )
        )

    warnings = []
    errors = []
    paper_2025 = GAOKAO_2009_2025 / "2025年高考数学试卷（上海）（秋考）（空白卷）.pdf"
    if not paper_2025.exists():
        errors.append(f"missing 2025 autumn blank paper PDF: {paper_2025}")
    answer_2025 = autumn_answer_pdf(2025, warnings, errors, prefer_answer=True)
    items.append(
        ImportItem(
            run_id="shanghai_2025_autumn_paper_answer",
            year=2025,
            season="autumn",
            mode="paper_plus_answer_file",
            source_shape="split_blank_answer",
            paper_pdf=existing(paper_2025),
            answer_pdf=answer_2025,
            paper_source_rule=r"2025 autumn paper from 2009_2025 blank PDF",
            answer_source_rule=r"2025 autumn answer from 2009_2025 answer PDF",
            warnings=warnings,
            errors=errors,
        )
    )
    return items


def safe_clear_run_dir(target: Path, allowed_root: Path) -> None:
    resolved_target = target.resolve()
    resolved_root = allowed_root.resolve()
    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        raise RuntimeError(f"refusing to clear path outside runs root: {target}")
    if target.exists():
        shutil.rmtree(target)


def page_numbers_from_range(page_range: str) -> list[int]:
    pages: list[int] = []
    for part in page_range.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"invalid descending page range: {page_range}")
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(part))
    if not pages:
        raise ValueError(f"empty page range: {page_range}")
    return pages


def split_pdf_pages(source_pdf: Path, page_range: str, out_pdf: Path) -> Path:
    if out_pdf.exists():
        return out_pdf
    reader = PdfReader(str(source_pdf))
    writer = PdfWriter()
    page_count = len(reader.pages)
    for page_no in page_numbers_from_range(page_range):
        if page_no < 1 or page_no > page_count:
            raise ValueError(f"page {page_no} out of range 1-{page_count} for {source_pdf}")
        writer.add_page(reader.pages[page_no - 1])
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with out_pdf.open("wb") as handle:
        writer.write(handle)
    return out_pdf


def extraction_pdf_for_part(item: ImportItem, run_dir: Path, part: str) -> Path:
    if part == "paper":
        source = item.paper_pdf
        page_range = item.paper_page_range
    elif part == "answer":
        source = item.answer_pdf
        page_range = item.answer_page_range
    else:
        raise ValueError(f"unknown extraction part: {part}")
    if not source:
        raise RuntimeError(f"missing {part} pdf for {item.run_id}")
    source_pdf = Path(source)
    if not page_range:
        return source_pdf
    safe_range = page_range.replace(",", "_").replace("-", "_")
    return split_pdf_pages(source_pdf, page_range, run_dir / "_split_sources" / f"{part}_pages_{safe_range}.pdf")


def extract_one_pdf(pdf: Path, out_dir: Path, args: argparse.Namespace, data_id: str) -> dict[str, Any]:
    mineru_args = argparse.Namespace(
        mineru_token_file=str(Path(args.mineru_token_file).resolve()),
        mineru_ocr=True,
        mineru_language="ch",
        enable_formula=True,
        enable_table=True,
        page_ranges=None,
        extra_formats=[],
        mineru_timeout=args.mineru_timeout,
        mineru_poll_interval=args.mineru_poll_interval,
        dpi=args.dpi,
    )
    return ep.extract_document_mineru_vlm(pdf, out_dir, mineru_args, data_id)


def extract_one_pdf_with_retries(
    pdf: Path,
    out_dir: Path,
    args: argparse.Namespace,
    data_id: str,
    allowed_root: Path,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, args.extract_retries + 1):
        try:
            return extract_one_pdf(pdf, out_dir, args, data_id)
        except Exception as exc:
            last_error = exc
            if attempt >= args.extract_retries:
                break
            if out_dir.exists() and not (out_dir / "ocr_blocks.json").exists():
                safe_clear_run_dir(out_dir, allowed_root)
            print(
                f"[mineru] {data_id} failed on attempt {attempt}/{args.extract_retries}: {exc}; "
                f"sleep {args.extract_retry_sleep}s then retry",
                flush=True,
            )
            time.sleep(args.extract_retry_sleep)
    raise RuntimeError(f"MinerU extraction failed after {args.extract_retries} attempts for {pdf}: {last_error}") from last_error


def record_timing(report_path: Path, run_id: str, stage: str, started: float, status: str, detail: dict[str, Any]) -> None:
    append_jsonl(
        report_path,
        {
            "run_id": run_id,
            "stage": stage,
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            **detail,
        },
    )


def extract_source_runs(items: list[ImportItem], args: argparse.Namespace) -> None:
    runs_root = Path(args.runs_root)
    source_root = runs_root / "source_runs"
    timing_path = runs_root / "reports" / "source_extraction_timing.jsonl"
    selected = select_items(items, args.run_id)
    for item in selected:
        if not item.ready:
            if args.skip_missing:
                continue
            raise RuntimeError(f"{item.run_id} is not ready: {item.errors}")
        run_dir = source_root / item.run_id
        if args.force_source:
            safe_clear_run_dir(run_dir, source_root)
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "source_item.json", asdict(item))

        started = time.monotonic()
        try:
            paper_pdf = extraction_pdf_for_part(item, run_dir, "paper")
            extract_one_pdf_with_retries(
                paper_pdf,
                run_dir / "paper",
                args,
                f"{item.run_id}_paper",
                source_root,
            )
            if item.answer_pdf:
                answer_pdf = extraction_pdf_for_part(item, run_dir, "answer")
                extract_one_pdf_with_retries(
                    answer_pdf,
                    run_dir / "answer",
                    args,
                    f"{item.run_id}_answer",
                    source_root,
                )
            record_timing(timing_path, item.run_id, "source_extract", started, "ok", {"mode": item.mode})
        except Exception as exc:
            record_timing(timing_path, item.run_id, "source_extract", started, "error", {"error": str(exc)})
            raise


def pipeline_command(item: ImportItem, args: argparse.Namespace) -> list[str]:
    runs_root = Path(args.runs_root)
    cmd = [
        sys.executable,
        str(WORKTREE_ROOT / "run_pipeline.py"),
        "--source-runs-root",
        str(runs_root / "source_runs"),
        "--runs-root",
        str(runs_root / "step2_exam_blocks"),
        "--qb-root",
        str(runs_root / "question_bank"),
        "--render-root",
        str(runs_root / "rendered_question_bank_mathjax"),
        "--step3-5-root",
        str(runs_root / "reviews_step3_5_latex_audit"),
        "--run-id",
        item.run_id,
        "--model",
        args.model,
        "--llm-provider",
        "siliconflow",
        "--timeout",
        str(args.timeout),
        "--max-workers",
        str(args.max_workers),
        "--tpm-limit",
        str(args.tpm_limit),
        "--mode",
        item.mode,
    ]
    if args.token_estimator_model:
        cmd.extend(["--token-estimator-model", args.token_estimator_model])
    cmd.extend(["--image-token-mode", args.image_token_mode])
    cmd.extend(["--tpm-output-reserve", str(args.tpm_output_reserve)])
    if args.token_budget_log:
        cmd.extend(["--token-budget-log", str(args.token_budget_log)])
    if args.token_budget_pool:
        cmd.extend(["--token-budget-pool", str(args.token_budget_pool)])
    if args.token_budget_verbose:
        cmd.append("--token-budget-verbose")
    for item_text in args.model_tpm_limit or []:
        cmd.extend(["--model-tpm-limit", item_text])
    for model in args.worker_model or []:
        cmd.extend(["--worker-model", model])
    if args.pipeline_force:
        cmd.append("--force")
    return cmd


def run_pipeline_items(items: list[ImportItem], args: argparse.Namespace) -> None:
    runs_root = Path(args.runs_root)
    timing_path = runs_root / "reports" / "pipeline_timing.jsonl"
    log_root = runs_root / "logs"
    selected = select_items(items, args.run_id)
    for item in selected:
        if not item.ready:
            if args.skip_missing:
                continue
            raise RuntimeError(f"{item.run_id} is not ready: {item.errors}")
        log_path = log_root / f"{item.run_id}_pipeline.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(
                pipeline_command(item, args),
                cwd=str(PROJECT_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        status = "ok" if proc.returncode == 0 else "error"
        record_timing(
            timing_path,
            item.run_id,
            "pipeline",
            started,
            status,
            {"mode": item.mode, "returncode": proc.returncode, "log": str(log_path)},
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pipeline failed for {item.run_id}; see {log_path}")


def select_items(items: list[ImportItem], run_ids: list[str] | None) -> list[ImportItem]:
    if not run_ids:
        return items
    wanted = set(run_ids)
    selected = [item for item in items if item.run_id in wanted]
    missing = sorted(wanted - {item.run_id for item in selected})
    if missing:
        raise RuntimeError(f"unknown run_id(s): {missing}")
    return selected


def write_manifest(items: list[ImportItem], runs_root: Path) -> Path:
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(PROJECT_ROOT),
        "runs_root": str(runs_root),
        "model": DEFAULT_MODEL,
        "counts": {
            "total": len(items),
            "ready": sum(1 for item in items if item.ready),
            "with_errors": sum(1 for item in items if not item.ready),
            "with_warnings": sum(1 for item in items if item.warnings),
        },
        "items": [asdict(item) | {"ready": item.ready} for item in items],
    }
    path = runs_root / "reports" / "import_manifest.json"
    write_json(path, payload)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="v11 batch import for Shanghai 2017-2026 spring/autumn papers.")
    parser.add_argument("--stage", choices=["manifest", "extract", "pipeline", "all"], default="manifest")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--run-id", action="append")
    parser.add_argument("--skip-missing", action="store_true")
    parser.add_argument("--force-source", action="store_true")
    parser.add_argument("--mineru-token-file", type=Path, default=PROJECT_ROOT / ".mineru_token")
    parser.add_argument("--mineru-timeout", type=int, default=900)
    parser.add_argument("--mineru-poll-interval", type=int, default=10)
    parser.add_argument("--extract-retries", type=int, default=3)
    parser.add_argument("--extract-retry-sleep", type=int, default=60)
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--tpm-limit", type=int, default=36_000)
    parser.add_argument("--token-estimator-model", default=None)
    parser.add_argument("--image-token-mode", choices=["auto", "processor", "formula"], default="auto")
    parser.add_argument("--tpm-output-reserve", type=int, default=2048)
    parser.add_argument("--token-budget-log", type=Path, default=None)
    parser.add_argument("--token-budget-pool", type=Path, default=None)
    parser.add_argument("--token-budget-verbose", action="store_true")
    parser.add_argument("--model-tpm-limit", action="append", default=[])
    parser.add_argument("--worker-model", action="append", default=[])
    parser.add_argument("--pipeline-force", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.runs_root.mkdir(parents=True, exist_ok=True)
    items = build_import_items()
    manifest_path = write_manifest(items, args.runs_root)
    print(f"manifest={manifest_path}", flush=True)
    print(
        f"items={len(items)} ready={sum(1 for item in items if item.ready)} "
        f"errors={sum(1 for item in items if not item.ready)} warnings={sum(1 for item in items if item.warnings)}",
        flush=True,
    )
    if args.stage in {"extract", "all"}:
        extract_source_runs(items, args)
    if args.stage in {"pipeline", "all"}:
        run_pipeline_items(items, args)


if __name__ == "__main__":
    main()
