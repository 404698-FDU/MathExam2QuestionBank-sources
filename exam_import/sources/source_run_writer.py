from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
import time
from typing import Any

from .mineru_extract import MineruExtractConfig, extract_pdf_with_retries, sanitize_data_id
from .page_assets import page_range_slug, split_pdf_pages


SOURCE_EXTRACTORS = {"local_pymupdf", "mineru_vlm"}


@dataclass(frozen=True)
class SourcePartPlan:
    part: str
    pdf_path: Path | None
    page_range: str
    extractor: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["pdf_path"] = str(self.pdf_path) if self.pdf_path else ""
        return payload


def looks_like_source_part(part_dir: Path) -> bool:
    return (part_dir / "ocr_blocks.json").exists() and (part_dir / "pages").exists()


def validate_source_part_dir(part_dir: Path) -> None:
    missing: list[str] = []
    if not (part_dir / "ocr_blocks.json").exists():
        missing.append("ocr_blocks.json")
    if not (part_dir / "pages").exists():
        missing.append("pages/")
    if missing:
        raise RuntimeError(f"Prepared source part is incomplete: {part_dir}. Missing: {', '.join(missing)}")


def staged_pdf_for_part(run_dir: Path, plan: SourcePartPlan, *, force: bool) -> Path:
    if plan.pdf_path is None:
        raise RuntimeError(f"Missing PDF path for source part: {plan.part}")
    pdf = plan.pdf_path.resolve()
    if not pdf.exists():
        raise FileNotFoundError(pdf)
    if not plan.page_range.strip():
        return pdf
    split_path = run_dir / "_split_sources" / f"{plan.part}_pages_{page_range_slug(plan.page_range)}.pdf"
    if force and split_path.exists():
        split_path.unlink()
    if split_path.exists():
        return split_path.resolve()
    return split_pdf_pages(pdf, plan.page_range, split_path)


def clear_existing_part_dir(part_dir: Path) -> None:
    if part_dir.exists():
        shutil.rmtree(part_dir)


def extract_part_from_pdf(
    *,
    source_runs_root: Path,
    run_dir: Path,
    plan: SourcePartPlan,
    mineru: MineruExtractConfig,
    reuse_existing_ocr: bool,
    force_source: bool,
) -> dict[str, Any]:
    out_dir = run_dir / plan.part
    if looks_like_source_part(out_dir):
        if reuse_existing_ocr:
            return {"part": plan.part, "status": "reused", "dir": str(out_dir), "extractor": plan.extractor}
        raise RuntimeError(f"{plan.part} OCR already exists; use reuse_existing_ocr or force_source: {out_dir}")
    if out_dir.exists():
        if force_source:
            clear_existing_part_dir(out_dir)
        else:
            raise RuntimeError(f"{plan.part} directory exists but OCR is incomplete; use force_source: {out_dir}")

    staged_pdf = staged_pdf_for_part(run_dir, plan, force=force_source)
    started = time.monotonic()
    result = extract_pdf_with_retries(
        pdf=staged_pdf,
        out_dir=out_dir,
        extractor=plan.extractor,
        mineru=mineru,
        data_id=sanitize_data_id(f"{run_dir.name}_{plan.part}"),
    )
    elapsed = round(time.monotonic() - started, 3)
    return {
        "part": plan.part,
        "status": "extracted",
        "dir": str(out_dir),
        "extractor": plan.extractor,
        "source_pdf": str(plan.pdf_path.resolve()) if plan.pdf_path else "",
        "staged_pdf": str(staged_pdf),
        "page_range": plan.page_range,
        "elapsed_seconds": elapsed,
        "page_count": int(result.get("page_count") or 0),
        "source_runs_root": str(source_runs_root),
    }
