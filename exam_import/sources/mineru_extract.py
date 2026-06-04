from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import time
import zipfile
from typing import Any

import requests

from exam_import.core.io import read_json, write_json

from .page_assets import render_pdf_pages


@dataclass(frozen=True)
class TextUnit:
    unit_id: str
    page: int
    bbox: list[float]
    text: str
    block_id: str
    order: int = 0


@dataclass(frozen=True)
class Figure:
    figure_id: str
    page: int
    bbox: list[float]
    path: str
    assigned_question_no: int | None = None


@dataclass(frozen=True)
class MineruExtractConfig:
    token_file: str = ""
    timeout: int = 900
    poll_interval: int = 10
    dpi: int = 144
    extract_retries: int = 3
    extract_retry_sleep: int = 60
    language: str = "ch"
    enable_formula: bool = True
    enable_table: bool = True
    enable_ocr: bool = True
    extra_formats: tuple[str, ...] = ()


def sanitize_data_id(value: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return cleaned[:128] or "paper"


def extract_pdf_with_retries(
    *,
    pdf: Path,
    out_dir: Path,
    extractor: str,
    mineru: MineruExtractConfig,
    data_id: str,
) -> dict[str, Any]:
    last_error: Exception | None = None
    attempts = 1 if extractor == "local_pymupdf" else max(1, mineru.extract_retries)
    for attempt in range(1, attempts + 1):
        try:
            if extractor == "local_pymupdf":
                return extract_document_local_pymupdf(pdf, out_dir, dpi=mineru.dpi)
            if extractor == "mineru_vlm":
                return extract_document_mineru_vlm(pdf, out_dir, config=mineru, data_id=data_id)
            raise ValueError(f"Unsupported extractor: {extractor}")
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(mineru.extract_retry_sleep)
    assert last_error is not None
    raise last_error


def extract_document_mineru_vlm(
    pdf: Path,
    out_dir: Path,
    *,
    config: MineruExtractConfig,
    data_id: str,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    token = load_mineru_token(Path(config.token_file).resolve() if config.token_file else None)

    result_path = out_dir / "mineru_result.json"
    extract_dir = out_dir / "mineru_extract"
    if result_path.exists() and mineru_extract_has_content_list(extract_dir):
        result_payload = read_json(result_path)
    else:
        result_payload = read_json(result_path) if result_path.exists() else None
        if not result_payload or not mineru_result_is_done(result_payload, pdf.name):
            submitted = submit_mineru_local_file(pdf, out_dir, token, config, sanitize_data_id(data_id))
            result_payload = poll_mineru_batch(
                out_dir=out_dir,
                token=token,
                batch_id=submitted["batch_id"],
                timeout_seconds=config.timeout,
                interval_seconds=config.poll_interval,
            )
        extract_dir = download_and_extract_mineru_zip(out_dir, result_payload, pdf.name)

    page_count, page_records = render_pdf_pages(pdf, out_dir, config.dpi)
    content_list_path = find_mineru_content_list(extract_dir)
    content_list_v2_path = find_optional_mineru_file(extract_dir, ["*_content_list_v2.json", "content_list_v2.json"])
    layout_json_path = find_optional_mineru_file(extract_dir, ["layout.json", "*_middle.json"])
    layout_pdf_path = find_optional_mineru_file(extract_dir, ["*layout*.pdf", "layout.pdf"])
    model_json_path = find_optional_mineru_file(extract_dir, ["*_model.json", "model.json"])
    content_items = read_json(content_list_path)
    if not isinstance(content_items, list):
        raise RuntimeError(f"Unexpected MinerU content list shape: {content_list_path}")

    text_units: list[TextUnit] = []
    figures: list[Figure] = []
    blocks: list[dict[str, Any]] = []
    unit_order = 0
    block_counter = 0
    figure_counter = 0
    figure_types = {"image", "chart"}
    skipped_text_types = {"header", "footer", "page_number", "aside_text", "page_footnote"}

    for item in content_items:
        if not isinstance(item, dict):
            continue
        block_counter += 1
        item_type = str(item.get("type") or "unknown")
        page = int(item.get("page_idx", 0)) + 1
        bbox = [round(float(v), 4) for v in item.get("bbox") or [0, 0, 1000, 1000]]
        block_id = f"mineru_p{page:03d}_b{block_counter:04d}"
        block_order_fields = {
            "order": block_counter,
            "reading_order": block_counter,
            "order_source": "mineru_content_list",
        }

        if item_type in figure_types and item.get("img_path"):
            figure_counter += 1
            fig_id = f"mineru_p{page:03d}_fig{figure_counter:04d}"
            img_path = str(item["img_path"]).replace("\\", "/")
            image_abs = (extract_dir / img_path).resolve()
            try:
                rel_path = str(image_abs.relative_to(out_dir.resolve()))
            except ValueError:
                rel_path = str(Path("mineru_extract") / img_path)
            figures.append(Figure(fig_id, page, bbox, rel_path))
            blocks.append(
                {
                    "block_id": block_id,
                    "type": item_type,
                    "page": page,
                    "bbox": bbox,
                    "figure_id": fig_id,
                    "path": rel_path,
                    "mineru_item": item,
                    **block_order_fields,
                }
            )
            continue

        text = mineru_item_text(item)
        blocks.append(
            {
                "block_id": block_id,
                "type": item_type,
                "page": page,
                "bbox": bbox,
                "text": text,
                "mineru_item": item,
                **block_order_fields,
            }
        )
        if item_type in skipped_text_types or not text.strip():
            continue
        for line_index, line in enumerate(str(text).splitlines() or [str(text)], start=1):
            if not line.strip():
                continue
            unit_order += 1
            unit_id = f"{block_id}_l{line_index:03d}"
            text_units.append(TextUnit(unit_id, page, bbox, line.strip(), block_id, unit_order))

    extracted = {
        "source_pdf": str(pdf.resolve()),
        "render_pdf": str(pdf.resolve()),
        "extractor": "mineru_vlm",
        "model_version": "vlm",
        "content_list": str(content_list_path.relative_to(out_dir)),
        "content_list_v2": str(content_list_v2_path.relative_to(out_dir)) if content_list_v2_path else None,
        "layout_json": str(layout_json_path.relative_to(out_dir)) if layout_json_path else None,
        "layout_pdf": str(layout_pdf_path.relative_to(out_dir)) if layout_pdf_path else None,
        "model_json": str(model_json_path.relative_to(out_dir)) if model_json_path else None,
        "page_count": page_count,
        "pages": page_records,
        "blocks": blocks,
        "text_units": [asdict(unit) for unit in text_units],
        "figures": [asdict(fig) for fig in figures],
    }
    write_json(out_dir / "ocr_blocks.json", extracted)
    return extracted


def extract_document_local_pymupdf(pdf: Path, out_dir: Path, *, dpi: int = 144) -> dict[str, Any]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for local PDF extraction") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf))
    pages_dir = out_dir / "pages"
    figures_dir = out_dir / "figures"
    pages_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)
    matrix = fitz.Matrix(dpi / 72, dpi / 72)

    text_units: list[TextUnit] = []
    figures: list[Figure] = []
    blocks: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []

    unit_order = 0
    for page_index, page in enumerate(doc, start=1):
        page_png = pages_dir / f"page_{page_index:03d}.png"
        if not page_png.exists():
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pix.save(str(page_png))
        page_records.append(
            {
                "page": page_index,
                "width_pt": round(page.rect.width, 2),
                "height_pt": round(page.rect.height, 2),
                "image": str(page_png.relative_to(out_dir)),
            }
        )

        page_dict = page.get_text("dict")
        block_counter = 0
        figure_counter = 0
        for block in page_dict.get("blocks", []):
            block_counter += 1
            block_id = f"p{page_index:03d}_b{block_counter:03d}"
            bbox = [round(float(v), 2) for v in block.get("bbox", [0, 0, 0, 0])]
            block_type = block.get("type")
            if block_type == 0:
                block_text_parts: list[str] = []
                for line_index, line in enumerate(block.get("lines", []), start=1):
                    spans = line.get("spans", [])
                    line_text = "".join(span.get("text", "") for span in spans).strip()
                    if not line_text:
                        continue
                    unit_id = f"{block_id}_l{line_index:03d}"
                    line_bbox = [round(float(v), 2) for v in line.get("bbox", bbox)]
                    unit_order += 1
                    text_units.append(TextUnit(unit_id, page_index, line_bbox, line_text, block_id, unit_order))
                    block_text_parts.append(line_text)
                blocks.append(
                    {
                        "block_id": block_id,
                        "type": "text",
                        "page": page_index,
                        "bbox": bbox,
                        "text": "\n".join(block_text_parts),
                    }
                )
                continue
            if block_type != 1:
                continue
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            if width < 18 or height < 18:
                continue
            figure_counter += 1
            fig_id = f"p{page_index:03d}_fig{figure_counter:03d}"
            fig_path = figures_dir / f"{fig_id}.png"
            clip = fitz.Rect(bbox)
            rel_path = ""
            try:
                fig_pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
                fig_pix.save(str(fig_path))
                rel_path = str(fig_path.relative_to(out_dir))
            except Exception:
                rel_path = ""
            figures.append(Figure(fig_id, page_index, bbox, rel_path))
            blocks.append(
                {
                    "block_id": block_id,
                    "type": "image",
                    "page": page_index,
                    "bbox": bbox,
                    "figure_id": fig_id,
                    "path": rel_path,
                }
            )

    extracted = {
        "source_pdf": str(pdf.resolve()),
        "extractor": "local_pymupdf",
        "page_count": doc.page_count,
        "pages": page_records,
        "blocks": blocks,
        "text_units": [asdict(unit) for unit in text_units],
        "figures": [asdict(fig) for fig in figures],
    }
    doc.close()
    write_json(out_dir / "ocr_blocks.json", extracted)
    return extracted


def load_mineru_token(token_file: Path | None) -> str:
    token = os.environ.get("MINERU_API_TOKEN", "").strip()
    if not token and token_file and token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1].strip()
    if not token:
        raise RuntimeError("MinerU token not found. Set MINERU_API_TOKEN or configure mineru.token_file.")
    return token


def mineru_headers(token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def redact_upload_urls(payload: Any) -> Any:
    if isinstance(payload, dict):
        result = {}
        for key, value in payload.items():
            if key == "file_urls" and isinstance(value, list):
                result[key] = [f"<redacted-upload-url-{idx + 1}>" for idx, _ in enumerate(value)]
            else:
                result[key] = redact_upload_urls(value)
        return result
    if isinstance(payload, list):
        return [redact_upload_urls(item) for item in payload]
    return payload


def check_api_response(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{action} returned non-JSON response: HTTP {response.status_code}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"{action} failed: HTTP {response.status_code}, body={payload}")
    if payload.get("code") != 0:
        raise RuntimeError(f"{action} failed: code={payload.get('code')}, msg={payload.get('msg')}")
    return payload


def submit_mineru_local_file(
    pdf: Path,
    out_dir: Path,
    token: str,
    config: MineruExtractConfig,
    data_id: str,
) -> dict[str, Any]:
    url = "https://mineru.net/api/v4/file-urls/batch"
    payload: dict[str, Any] = {
        "files": [
            {
                "name": pdf.name,
                "data_id": data_id,
                "is_ocr": config.enable_ocr,
            }
        ],
        "model_version": "vlm",
        "language": config.language,
        "enable_formula": config.enable_formula,
        "enable_table": config.enable_table,
    }
    if config.extra_formats:
        payload["extra_formats"] = list(config.extra_formats)

    response = requests.post(url, headers=mineru_headers(token), json=payload, timeout=60)
    result = check_api_response(response, "MinerU upload-url request")
    write_json(out_dir / "mineru_submit_response_redacted.json", redact_upload_urls(result))

    data = result["data"]
    batch_id = data["batch_id"]
    file_urls = data["file_urls"]
    if not file_urls:
        raise RuntimeError("MinerU did not return an upload URL")

    with pdf.open("rb") as handle:
        upload_response = requests.put(file_urls[0], data=handle, timeout=180)
    if upload_response.status_code != 200:
        raise RuntimeError(f"MinerU file upload failed: HTTP {upload_response.status_code}")
    write_json(out_dir / "mineru_upload_status.json", {"batch_id": batch_id, "status_code": upload_response.status_code})
    return {"batch_id": batch_id, "file_name": pdf.name, "data_id": data_id}


def poll_mineru_batch(
    *,
    out_dir: Path,
    token: str,
    batch_id: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> dict[str, Any]:
    url = f"https://mineru.net/api/v4/extract-results/batch/{batch_id}"
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = requests.get(url, headers=mineru_headers(token), timeout=60)
        payload = check_api_response(response, "MinerU batch-result request")
        last_payload = payload
        write_json(out_dir / "mineru_poll_latest.json", payload)
        results = payload.get("data", {}).get("extract_result", [])
        if not isinstance(results, list):
            raise RuntimeError("MinerU result payload has unexpected extract_result shape")
        if results and all(item.get("state") in {"done", "failed"} for item in results):
            write_json(out_dir / "mineru_result.json", payload)
            return payload
        time.sleep(interval_seconds)
    write_json(out_dir / "mineru_poll_timeout.json", last_payload or {"batch_id": batch_id})
    raise TimeoutError(f"MinerU batch {batch_id} did not finish within {timeout_seconds} seconds")


def download_and_extract_mineru_zip(out_dir: Path, result_payload: dict[str, Any], expected_file_name: str) -> Path:
    results = result_payload.get("data", {}).get("extract_result", [])
    matching = [item for item in results if item.get("file_name") == expected_file_name]
    item = matching[0] if matching else results[0] if results else None
    if not item:
        raise RuntimeError("MinerU result is empty")
    if item.get("state") != "done":
        raise RuntimeError(f"MinerU parse failed or incomplete: state={item.get('state')}, err={item.get('err_msg')}")
    zip_url = item.get("full_zip_url")
    if not zip_url:
        raise RuntimeError("MinerU result does not contain full_zip_url")

    zip_path = out_dir / "mineru_result.zip"
    extract_dir = out_dir / "mineru_extract"
    extract_dir.mkdir(parents=True, exist_ok=True)
    if zip_path.exists() and not zipfile.is_zipfile(zip_path):
        zip_path.unlink()
    if not zip_path.exists():
        partial_path = zip_path.with_suffix(".zip.part")
        for attempt in range(1, 6):
            try:
                with requests.get(zip_url, stream=True, timeout=240) as response:
                    response.raise_for_status()
                    with partial_path.open("wb") as out_file:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                out_file.write(chunk)
                partial_path.replace(zip_path)
                break
            except requests.RequestException as exc:
                if partial_path.exists():
                    partial_path.unlink()
                if attempt >= 5:
                    raise RuntimeError(f"MinerU zip download failed after {attempt} attempts: {exc}") from exc
                time.sleep(3 * attempt)
    if not any(extract_dir.iterdir()):
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)
    return extract_dir


def mineru_extract_has_content_list(extract_dir: Path) -> bool:
    return extract_dir.exists() and bool(list(extract_dir.rglob("*content_list*.json")))


def mineru_result_is_done(result_payload: dict[str, Any], expected_file_name: str) -> bool:
    results = result_payload.get("data", {}).get("extract_result", [])
    matching = [item for item in results if item.get("file_name") == expected_file_name]
    item = matching[0] if matching else results[0] if results else None
    return bool(item and item.get("state") == "done" and item.get("full_zip_url"))


def find_mineru_content_list(extract_dir: Path) -> Path:
    candidates = sorted(extract_dir.rglob("*_content_list.json"))
    if not candidates:
        candidates = sorted(extract_dir.rglob("content_list.json"))
    if not candidates:
        raise RuntimeError(f"No MinerU content_list JSON found under {extract_dir}")
    non_v2 = [path for path in candidates if "content_list_v2" not in path.name]
    return non_v2[0] if non_v2 else candidates[0]


def find_optional_mineru_file(extract_dir: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(extract_dir.rglob(pattern))
        if matches:
            return matches[0]
    return None


def mineru_item_text(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type in {"text", "title", "equation"}:
        return str(item.get("text") or item.get("content") or "")
    if item_type == "table":
        parts = []
        parts.extend(item.get("table_caption") or [])
        body = item.get("table_body") or item.get("content") or ""
        if body:
            parts.append(str(body))
        parts.extend(item.get("table_footnote") or [])
        return "\n".join(str(part) for part in parts if part)
    if item_type in {"list", "index"}:
        return "\n".join(str(part) for part in item.get("list_items") or [])
    if item_type in {"code", "algorithm"}:
        return str(item.get("code_body") or item.get("algorithm_content") or "")
    return ""
