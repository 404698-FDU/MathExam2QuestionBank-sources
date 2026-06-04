from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from exam_import.core.io import write_json


PAGE_RANGE_RE = re.compile(r"^\s*\d+\s*(?:-\s*\d+\s*)?(?:,\s*\d+\s*(?:-\s*\d+\s*)?)*$")


def page_range_slug(page_range: str) -> str:
    return page_range.replace(",", "_").replace("-", "_").replace(" ", "")


def parse_page_range(page_range: str, page_count: int) -> list[int]:
    text = page_range.strip()
    if not text:
        return list(range(1, page_count + 1))
    if not PAGE_RANGE_RE.match(text):
        raise ValueError(f"Invalid page range syntax: {page_range!r}")
    pages: list[int] = []
    for chunk in text.split(","):
        part = chunk.strip()
        if "-" in part:
            left_text, right_text = [item.strip() for item in part.split("-", 1)]
            left = int(left_text)
            right = int(right_text)
            if left > right:
                raise ValueError(f"Invalid descending page range: {part!r}")
            pages.extend(range(left, right + 1))
        else:
            pages.append(int(part))
    normalized: list[int] = []
    seen: set[int] = set()
    for page in pages:
        if page < 1 or page > page_count:
            raise ValueError(f"Page {page} out of bounds for document with {page_count} pages")
        if page in seen:
            continue
        seen.add(page)
        normalized.append(page)
    return normalized


def split_pdf_pages(pdf: Path, page_range: str, out_pdf: Path) -> Path:
    if not page_range.strip():
        return pdf.resolve()
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to split PDF pages") from exc

    src = fitz.open(str(pdf))
    pages = parse_page_range(page_range, src.page_count)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    if out_pdf.exists():
        out_pdf.unlink()
    dst = fitz.open()
    for page_no in pages:
        dst.insert_pdf(src, from_page=page_no - 1, to_page=page_no - 1)
    dst.save(str(out_pdf))
    dst.close()
    src.close()
    return out_pdf.resolve()


def render_pdf_pages(pdf_path: Path, out_dir: Path, dpi: int) -> tuple[int, list[dict[str, Any]]]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to render PDF pages") from exc

    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    page_records: list[dict[str, Any]] = []
    for page_index, page in enumerate(doc, start=1):
        page_png = pages_dir / f"page_{page_index:03d}.png"
        if not page_png.exists():
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pix.save(str(page_png))
        page_records.append(
            {
                "page": page_index,
                "width_pt": 1000.0,
                "height_pt": 1000.0,
                "source_width_pt": round(page.rect.width, 2),
                "source_height_pt": round(page.rect.height, 2),
                "image": str(page_png.relative_to(out_dir)),
            }
        )
    page_count = doc.page_count
    doc.close()
    return page_count, page_records


def write_ocr_payload(path: Path, payload: dict[str, Any]) -> None:
    write_json(path, payload)
