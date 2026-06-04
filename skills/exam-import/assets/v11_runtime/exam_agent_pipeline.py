from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import statistics
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

try:
    import fitz  # PyMuPDF
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyMuPDF is required: python -m pip install pymupdf") from exc


BLANK_TOKEN = "\u7a7a\u767d\u5377"
ANSWER_TOKEN = "\u7b54\u6848\u5377"
SOLUTION_TOKEN = "\u89e3\u6790\u5377"
QUESTION_START_HINT_RE = (
    r"(?:不等式|已知|求|如图|若|下列|根据|设|在|空间|判断|点|函数|直线|以|将|某|过|一个|上海|计算|方程|集合|命题|曲线|证明)"
)
QUESTION_RE = re.compile(
    r"^\s*(\d{1,2})\s*(?:(?:[.](?!\d))|[\uFF0E\u3001\u3002]|[,，](?=\s*"
    + QUESTION_START_HINT_RE
    + r")|[)\uFF09]|(?:[\(\uFF08](?=\u672c\u9898)))\s*(.*)"
)
ANSWER_QUESTION_RE = re.compile(
    r"^\s*[\(\uFF08]?\s*(\d{1,2})\s*(?:[.\uFF0E\u3001\u3002]|[,，](?=\s*"
    + QUESTION_START_HINT_RE
    + r")|[)\uFF09]|(?:[\(\uFF08](?=\u672c\u9898))|(?="
    + QUESTION_START_HINT_RE
    + r"))\s*(.*)"
)
OPTION_RE = re.compile(r"^\s*([ABCD])\s*(?:[.\uFF0E\u3001]|[)\uFF09])\s*(.*)")
OPTION_MARKER_RE = re.compile(r"(?<![A-Za-z0-9])([ABCD])\s*(?:[.\uFF0E\u3001]|[)\uFF09])")
LOOSE_OPTION_MARKER_RE = re.compile(r"(?<![A-Za-z0-9])([ABCD])\s+(?=(?:\$|\\|[\u4e00-\u9fff]|[0-9]|[（(]))")
SECTION_RE = re.compile(r"^\s*[\u4e00\u4e8c\u4e09\u56db]\s*[\u3001,.]")
EMBEDDED_QUESTION_START_RE = re.compile(
    r"(?<![\dA-Za-z])(\d{1,2})\s*(?:(?:[.](?!\d))|[\uFF0E\u3001\u3002,，])\s*"
    r"(?=" + QUESTION_START_HINT_RE + r")"
)
WATERMARK_MARKERS = (
    "REPLiX-DB",
    "replix-db",
    "linkium",
    "nlrdev",
    "\u745e\u8054\u4e07\u8c61",
    "\u672a\u6765\u6559\u7814\u4e4b\u661f",
    "\u4e25\u7981\u7528\u4e8e\u5546\u4e1a",
)
SECTION_HEADING_TOKENS = (
    "\u586b\u7a7a\u9898",
    "\u771f\u7a7a\u9898",
    "\u586b\u7a7a",
    "\u9009\u62e9\u9898",
    "\u89e3\u7b54\u9898",
)


@dataclass
class TextUnit:
    unit_id: str
    page: int
    bbox: list[float]
    text: str
    block_id: str
    order: int = 0


@dataclass
class Figure:
    figure_id: str
    page: int
    bbox: list[float]
    path: str
    assigned_question_no: int | None = None


@dataclass
class Question:
    question_no: int
    question_type: str
    raw_text: str
    normalized_text: str
    source_units: list[str]
    source_pages: list[int]
    source_bboxes: dict[str, list[float]]
    options: dict[str, str] = field(default_factory=dict)
    figures: list[str] = field(default_factory=list)
    answer_raw: str | None = None
    confidence: float = 0.0
    flags: list[str] = field(default_factory=list)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_pdf_text_len(pdf: Path) -> tuple[int, int, int]:
    doc = fitz.open(str(pdf))
    text_len = 0
    image_count = 0
    for page in doc:
        text_len += len(page.get_text("text"))
        image_count += len(page.get_images(full=True))
    return doc.page_count, text_len, image_count


def clean_pair_key(path: Path) -> str:
    stem = path.stem
    for token in (BLANK_TOKEN, ANSWER_TOKEN, SOLUTION_TOKEN):
        stem = stem.replace(token, "")
    stem = re.sub(r"[\(\)\uFF08\uFF09]+", "", stem)
    return re.sub(r"\s+", "", stem)


def discover_pairs(data_dir: Path) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for pdf in sorted(data_dir.glob("*.pdf")):
        key = clean_pair_key(pdf)
        item = grouped.setdefault(key, {"key": key, "blank": None, "answer": None, "solution": None})
        name = pdf.name
        if BLANK_TOKEN in name:
            item["blank"] = str(pdf)
        elif ANSWER_TOKEN in name:
            item["answer"] = str(pdf)
        elif SOLUTION_TOKEN in name:
            item["solution"] = str(pdf)
    pairs = []
    for item in grouped.values():
        if item["blank"]:
            item["answer_like"] = item["answer"] or item["solution"]
            pairs.append(item)
    return pairs


def select_pair(pairs: list[dict[str, Any]], year: int | None) -> dict[str, Any]:
    candidates = pairs
    if year is not None:
        candidates = [p for p in candidates if str(year) in Path(p["blank"]).name]
    if not candidates:
        raise SystemExit(f"No blank paper pair found for year={year!r}")

    def score(item: dict[str, Any]) -> tuple[int, int, str]:
        name = Path(item["blank"]).name
        m = re.search(r"(20\d{2})", name)
        parsed_year = int(m.group(1)) if m else 0
        has_answer = 1 if item.get("answer_like") else 0
        return parsed_year, has_answer, name

    return sorted(candidates, key=score)[-1]


def is_noise_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if any(marker in stripped for marker in WATERMARK_MARKERS):
        return True
    if re.fullmatch(r"\d{1,2}\s*[、,，]\s*\d{1,2}", stripped):
        return True
    if re.search(r"\u6570\u5b66\u8bd5\u5377\s+\u7b2c\s*\d+\s*\u9875", stripped):
        return True
    return False


def is_exam_section_heading(text: str) -> bool:
    return exam_section_kind(text) is not None


def exam_section_kind(text: str) -> str | None:
    stripped = text.strip()
    if re.fullmatch(r"[一二三四五六七八九十]\s*[、,.．。]+\s*(填空|选择|解答|答案).*", stripped):
        if "填空" in stripped or "真空" in stripped:
            return "fill_blank"
        if "选择" in stripped:
            return "single_choice"
        if "解答" in stripped:
            return "solution"
        if "答案" in stripped:
            return "answer"
    if re.match(r"^[一二三四五六七八九十]\s*[、,.．。]+", stripped):
        if any(token in stripped for token in ("填空", "真空")):
            return "fill_blank"
        if "选择" in stripped:
            return "single_choice"
        if "解答" in stripped:
            return "solution"
        if "答案" in stripped:
            return "answer"
    if stripped.startswith(("填空题", "真空题", "填空")):
        return "fill_blank"
    if stripped.startswith("选择题"):
        return "single_choice"
    if stripped.startswith("解答题"):
        return "solution"
    return None


def is_page_number_unit(unit: TextUnit) -> bool:
    return bool(re.fullmatch(r"\d+", unit.text.strip()) and unit.bbox[1] < 120)


def normalize_text(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("________", r"\blank")
    text = text.replace("_______", r"\blank")
    text = text.replace("______", r"\blank")
    text = text.replace("\u2264", r"\le ")
    text = text.replace("\u2265", r"\ge ")
    text = text.replace("\u2260", r"\ne ")
    text = text.replace("\u2212", "-")
    text = text.replace("\u00d7", r"\times ")
    text = text.replace("\u00b0", r"^\circ ")
    return text.strip()


def sanitize_data_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return cleaned[:128] or "paper"


def load_mineru_token(token_file: Path | None) -> str:
    token = os.environ.get("MINERU_API_TOKEN", "").strip()
    if not token and token_file and token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1].strip()
    if not token:
        raise SystemExit("MinerU token not found. Set MINERU_API_TOKEN or provide --mineru-token-file.")
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
    args: argparse.Namespace,
    data_id: str,
) -> dict[str, Any]:
    url = "https://mineru.net/api/v4/file-urls/batch"
    file_spec: dict[str, Any] = {
        "name": pdf.name,
        "data_id": data_id,
        "is_ocr": args.mineru_ocr,
    }
    if args.page_ranges:
        file_spec["page_ranges"] = args.page_ranges
    payload: dict[str, Any] = {
        "files": [file_spec],
        "model_version": "vlm",
        "language": args.mineru_language,
        "enable_formula": args.enable_formula,
        "enable_table": args.enable_table,
    }
    if args.extra_formats:
        payload["extra_formats"] = args.extra_formats

    response = requests.post(url, headers=mineru_headers(token), json=payload, timeout=60)
    result = check_api_response(response, "MinerU upload-url request")
    write_json(out_dir / "mineru_submit_response_redacted.json", redact_upload_urls(result))

    data = result["data"]
    batch_id = data["batch_id"]
    file_urls = data["file_urls"]
    if not file_urls:
        raise RuntimeError("MinerU did not return an upload URL.")

    with pdf.open("rb") as file_obj:
        upload_response = requests.put(file_urls[0], data=file_obj, timeout=180)
    if upload_response.status_code != 200:
        raise RuntimeError(f"MinerU file upload failed: HTTP {upload_response.status_code}")
    write_json(out_dir / "mineru_upload_status.json", {"batch_id": batch_id, "status_code": upload_response.status_code})
    return {"batch_id": batch_id, "file_name": pdf.name, "data_id": data_id}


def poll_mineru_batch(out_dir: Path, token: str, batch_id: str, timeout_seconds: int, interval_seconds: int) -> dict[str, Any]:
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
            raise RuntimeError("MinerU result payload has unexpected extract_result shape.")
        if results and all(item.get("state") in {"done", "failed"} for item in results):
            write_json(out_dir / "mineru_result.json", payload)
            return payload
        states = ", ".join(str(item.get("state")) for item in results) if results else "no-result-yet"
        print(f"MinerU batch {batch_id}: {states}")
        time.sleep(interval_seconds)
    write_json(out_dir / "mineru_poll_timeout.json", last_payload or {"batch_id": batch_id})
    raise TimeoutError(f"MinerU batch {batch_id} did not finish within {timeout_seconds} seconds.")


def download_and_extract_mineru_zip(out_dir: Path, result_payload: dict[str, Any], expected_file_name: str) -> Path:
    results = result_payload.get("data", {}).get("extract_result", [])
    matching = [item for item in results if item.get("file_name") == expected_file_name]
    item = matching[0] if matching else results[0] if results else None
    if not item:
        raise RuntimeError("MinerU result is empty.")
    if item.get("state") != "done":
        raise RuntimeError(f"MinerU parse failed or incomplete: state={item.get('state')}, err={item.get('err_msg')}")
    zip_url = item.get("full_zip_url")
    if not zip_url:
        raise RuntimeError("MinerU result does not contain full_zip_url.")

    zip_path = out_dir / "mineru_result.zip"
    extract_dir = ensure_dir(out_dir / "mineru_extract")
    if zip_path.exists() and not zipfile.is_zipfile(zip_path):
        zip_path.unlink()
    if not zip_path.exists():
        partial_path = zip_path.with_suffix(".zip.part")
        last_error: Exception | None = None
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
                last_error = exc
                if partial_path.exists():
                    partial_path.unlink()
                if attempt == 5:
                    raise RuntimeError(f"MinerU zip download failed after {attempt} attempts: {exc}") from exc
                time.sleep(3 * attempt)
        if last_error and not zip_path.exists():
            raise RuntimeError(f"MinerU zip download failed: {last_error}") from last_error
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
    # Prefer the legacy flat content list over v2 because the downstream prototype expects a stream.
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


def render_pdf_pages(pdf_path: Path, out_dir: Path, dpi: int) -> tuple[int, list[dict[str, Any]]]:
    pages_dir = ensure_dir(out_dir / "pages")
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


def mineru_renderable_pdf_for_non_pdf(extract_dir: Path) -> Path:
    patterns = ["*_origin.pdf", "origin.pdf", "*layout*.pdf", "layout.pdf"]
    for pattern in patterns:
        matches = sorted(extract_dir.rglob(pattern))
        if matches:
            return matches[0]
    raise RuntimeError(f"MinerU non-PDF extraction did not include a renderable PDF under {extract_dir}")


def extract_document_mineru_vlm(pdf: Path, out_dir: Path, args: argparse.Namespace, data_id: str) -> dict[str, Any]:
    ensure_dir(out_dir)
    token = load_mineru_token(Path(args.mineru_token_file) if args.mineru_token_file else None)

    if (out_dir / "mineru_result.json").exists() and mineru_extract_has_content_list(out_dir / "mineru_extract"):
        result_payload = read_json(out_dir / "mineru_result.json")
        extract_dir = out_dir / "mineru_extract"
    else:
        result_payload = read_json(out_dir / "mineru_result.json") if (out_dir / "mineru_result.json").exists() else None
        if not result_payload or not mineru_result_is_done(result_payload, pdf.name):
            submitted = submit_mineru_local_file(pdf, out_dir, token, args, data_id)
            result_payload = poll_mineru_batch(out_dir, token, submitted["batch_id"], args.mineru_timeout, args.mineru_poll_interval)
        extract_dir = download_and_extract_mineru_zip(out_dir, result_payload, pdf.name)

    # Keep page images local and stable for the review UI even when MinerU supplies only structured data.
    is_pdf = pdf.suffix.lower() == ".pdf"
    if is_pdf:
        render_pdf = pdf
    else:
        render_pdf = mineru_renderable_pdf_for_non_pdf(extract_dir)
    page_count, page_records = render_pdf_pages(render_pdf, out_dir, args.dpi)

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
        "source_pdf": str(pdf),
        "render_pdf": str(render_pdf),
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


def extract_document_local(pdf: Path, out_dir: Path, dpi: int = 144) -> dict[str, Any]:
    return extract_document(pdf, out_dir, dpi=dpi)


def union_bbox(existing: list[float] | None, bbox: list[float]) -> list[float]:
    if existing is None:
        return bbox[:]
    return [
        min(existing[0], bbox[0]),
        min(existing[1], bbox[1]),
        max(existing[2], bbox[2]),
        max(existing[3], bbox[3]),
    ]


def extract_document(pdf: Path, out_dir: Path, dpi: int = 144) -> dict[str, Any]:
    doc = fitz.open(str(pdf))
    pages_dir = ensure_dir(out_dir / "pages")
    figures_dir = ensure_dir(out_dir / "figures")
    matrix = fitz.Matrix(dpi / 72, dpi / 72)

    text_units: list[TextUnit] = []
    figures: list[Figure] = []
    blocks: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []

    unit_order = 0
    for page_index, page in enumerate(doc, start=1):
        page_png = pages_dir / f"page_{page_index:03d}.png"
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
            elif block_type == 1:
                width = bbox[2] - bbox[0]
                height = bbox[3] - bbox[1]
                if width < 18 or height < 18:
                    continue
                figure_counter += 1
                fig_id = f"p{page_index:03d}_fig{figure_counter:03d}"
                fig_path = figures_dir / f"{fig_id}.png"
                clip = fitz.Rect(bbox)
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
        "source_pdf": str(pdf),
        "extractor": "local_pymupdf",
        "page_count": doc.page_count,
        "pages": page_records,
        "blocks": blocks,
        "text_units": [asdict(unit) for unit in text_units],
        "figures": [asdict(fig) for fig in figures],
    }
    write_json(out_dir / "ocr_blocks.json", extracted)
    return extracted


def sorted_text_units(extracted: dict[str, Any]) -> list[TextUnit]:
    units = [TextUnit(**item) for item in extracted["text_units"]]
    return sorted(units, key=lambda u: u.order)


def split_embedded_question_units(units: list[TextUnit]) -> list[TextUnit]:
    expanded: list[TextUnit] = []
    for unit in units:
        text = unit.text
        starts = [
            match.start()
            for match in EMBEDDED_QUESTION_START_RE.finditer(text)
            if match.start() > 0 and is_embedded_question_boundary(text, match.start())
        ]
        if not starts:
            expanded.append(unit)
            continue
        boundaries = [0, *starts, len(text)]
        for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            part = text[start:end].strip()
            if not part:
                continue
            expanded.append(
                TextUnit(
                    unit_id=unit.unit_id,
                    page=unit.page,
                    bbox=unit.bbox,
                    text=part,
                    block_id=unit.block_id,
                    order=unit.order * 100 + index,
                )
            )
    return expanded


def is_embedded_question_boundary(text: str, start: int) -> bool:
    prefix = text[:start].rstrip()
    if not prefix:
        return False
    return prefix[-1] not in "=<>+-*/"


def looks_like_question_stem_start(text: str) -> bool:
    stripped = text.strip()
    return bool(re.match(QUESTION_START_HINT_RE, stripped))


def classify_question(no: int, text: str, options: dict[str, str], section_kind: str | None = None) -> str:
    if options:
        return "single_choice"
    if section_kind in {"fill_blank", "single_choice", "solution"}:
        return section_kind
    if no <= 12 or "________" in text or r"\blank" in text:
        return "fill_blank"
    if no >= 17:
        return "solution"
    return "unknown"


def extract_options(question_text: str, question_no: int) -> dict[str, str]:
    if not 13 <= question_no <= 18:
        return {}
    lines = [line.strip() for line in question_text.splitlines() if line.strip()]
    flat = " ".join(lines)
    inline_matches = list(OPTION_MARKER_RE.finditer(flat))
    inline_labels = {match.group(1) for match in inline_matches}
    if len(inline_labels) >= 3:
        inline_options: dict[str, str] = {}
        for index, match in enumerate(inline_matches):
            label = match.group(1)
            end = inline_matches[index + 1].start() if index + 1 < len(inline_matches) else len(flat)
            value = flat[match.end() : end].strip(" \t:：;；,，()（）")
            inline_options[label] = normalize_text(value)
        return inline_options
    loose_matches = list(LOOSE_OPTION_MARKER_RE.finditer(flat))
    loose_labels = {match.group(1) for match in loose_matches}
    if len(loose_labels) >= 4:
        loose_options: dict[str, str] = {}
        for index, match in enumerate(loose_matches):
            label = match.group(1)
            end = loose_matches[index + 1].start() if index + 1 < len(loose_matches) else len(flat)
            value = flat[match.end() : end].strip(" \t:：;；,，()（）")
            loose_options[label] = normalize_text(value)
        if all(loose_options.get(label) for label in "ABCD"):
            return loose_options

    options: dict[str, list[str]] = {}
    current_label: str | None = None
    for line in lines:
        match = OPTION_RE.match(line)
        if match:
            current_label = match.group(1)
            options[current_label] = [match.group(2).strip()]
            continue
        if current_label and not QUESTION_RE.match(line) and not SECTION_RE.match(line):
            options[current_label].append(line)
    return {label: normalize_text("\n".join(parts)) for label, parts in options.items()}


def build_question(
    source_units: list[TextUnit],
    question_re: re.Pattern[str] = QUESTION_RE,
    section_kind: str | None = None,
) -> Question | None:
    if not source_units:
        return None
    first_text = source_units[0].text
    match = question_re.match(first_text)
    if not match:
        return None
    no = int(match.group(1))
    raw_lines = []
    for idx, unit in enumerate(source_units):
        text = unit.text.strip()
        if idx == 0:
            text = question_re.sub(lambda m: m.group(2), text, count=1).strip()
        if is_noise_line(text) or is_exam_section_heading(text):
            continue
        raw_lines.append(text)
    raw_text = "\n".join(raw_lines).strip()
    normalized = normalize_text(raw_text)
    options = extract_options(raw_text, no)
    source_bboxes: dict[str, list[float]] = {}
    for unit in source_units:
        key = str(unit.page)
        source_bboxes[key] = union_bbox(source_bboxes.get(key), unit.bbox)
    qtype = classify_question(no, normalized, options, section_kind=section_kind)
    return Question(
        question_no=no,
        question_type=qtype,
        raw_text=raw_text,
        normalized_text=normalized,
        source_units=[u.unit_id for u in source_units],
        source_pages=sorted({u.page for u in source_units}),
        source_bboxes=source_bboxes,
        options=options,
    )


def segment_questions(extracted: dict[str, Any], answer_mode: bool = False) -> list[Question]:
    question_re = ANSWER_QUESTION_RE if answer_mode else QUESTION_RE
    questions: list[Question] = []
    current: list[TextUnit] = []
    current_no: int | None = None
    current_section: str | None = None
    seen_exam_section = False
    units = split_embedded_question_units(sorted_text_units(extracted))
    for unit in units:
        text = unit.text.strip()
        if is_noise_line(text) or is_page_number_unit(unit):
            continue
        section_kind = exam_section_kind(text)
        if section_kind:
            built = build_question(current, question_re=question_re, section_kind=current_section)
            if built:
                questions.append(built)
            current = []
            if answer_mode:
                current_no = None
            current_section = section_kind if section_kind != "answer" else current_section
            seen_exam_section = True
            continue
        match = question_re.match(text)
        candidate_no = int(match.group(1)) if match else None
        if not seen_exam_section and match and candidate_no == 1 and looks_like_question_stem_start(match.group(2)):
            seen_exam_section = True
        is_start = (
            seen_exam_section
            and
            candidate_no is not None
            and 1 <= candidate_no <= 30
            and (current_no is None or candidate_no > current_no)
        )
        if is_start:
            built = build_question(current, question_re=question_re, section_kind=current_section)
            if built:
                questions.append(built)
            current = [unit]
            current_no = candidate_no
        elif current:
            current.append(unit)
    built = build_question(current, question_re=question_re, section_kind=current_section)
    if built:
        questions.append(built)
    return questions


def assign_figures(questions: list[Question], extracted: dict[str, Any]) -> list[Figure]:
    figures = [Figure(**item) for item in extracted["figures"]]
    starts_by_page: dict[int, list[tuple[float, int]]] = {}
    page_heights = {page["page"]: page["height_pt"] for page in extracted["pages"]}
    for question in questions:
        for page_str, bbox in question.source_bboxes.items():
            starts_by_page.setdefault(int(page_str), []).append((bbox[1], question.question_no))
    for page_starts in starts_by_page.values():
        page_starts.sort()

    q_by_no = {q.question_no: q for q in questions}
    for fig in figures:
        starts = starts_by_page.get(fig.page, [])
        if not starts:
            continue
        fig_mid_y = (fig.bbox[1] + fig.bbox[3]) / 2
        assigned_no = None
        for idx, (start_y, qno) in enumerate(starts):
            end_y = starts[idx + 1][0] if idx + 1 < len(starts) else page_heights.get(fig.page, 10**9)
            if start_y <= fig_mid_y < end_y:
                assigned_no = qno
                break
        if assigned_no is None:
            assigned_no = min(starts, key=lambda item: abs(item[0] - fig_mid_y))[1]
        fig.assigned_question_no = assigned_no
        q_by_no[assigned_no].figures.append(fig.figure_id)
    return figures


ANSWER_MARKER_RE = re.compile(r"^\s*(\d{1,2})\s*[.．、,，]?\s*【答案】\s*(.*)$")
ANALYSIS_MARKER_RE = re.compile(r"^\s*【解析】\s*(.*)$")
NUMBERED_LINE_RE = re.compile(r"^\s*(\d{1,2})\s*[.．、,，]")


def parse_marked_answers(answer_questions: list[Question]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for question in answer_questions:
        lines = [line.strip() for line in question.normalized_text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            match = ANSWER_MARKER_RE.match(line)
            if not match:
                continue
            question_no = int(match.group(1))
            parts: list[str] = []
            answer_text = match.group(2).strip()
            if answer_text:
                parts.append(answer_text)
            next_index = index + 1
            if next_index < len(lines):
                analysis_match = ANALYSIS_MARKER_RE.match(lines[next_index])
                if analysis_match:
                    analysis_text = analysis_match.group(1).strip()
                    parts.append("【解析】" + analysis_text if analysis_text else "【解析】")
                    next_index += 1
                    while next_index < len(lines):
                        next_line = lines[next_index]
                        if ANSWER_MARKER_RE.match(next_line) or NUMBERED_LINE_RE.match(next_line):
                            break
                        parts.append(next_line)
                        next_index += 1
            if parts:
                answer = "\n".join(parts).strip()
                if question_no <= 18:
                    answer = trim_objective_answer_contamination(answer)
                answers[question_no] = answer
    return answers


def first_section_index(text: str) -> int:
    indices = [index for marker in ("【解析】", "【点评】", "【分析】", "【说明】") if (index := text.find(marker)) >= 0]
    return min(indices) if indices else -1


ANSWER_CONTAMINATION_START_RE = re.compile(
    r"^(?:已知|若|某|如图|函数|过点|点|直线|圆|椭圆|抛物线|数列|向量|集合|曲线|下列|根据|在|从|设|计算|证明)"
)
ANSWER_CONTAMINATION_PROMPT_RE = re.compile(
    r"(?:取值范围|满足的条件|关系式|最小值|最大值|概率|半径之比|大小|体积|值为|路程的和最短|报刊零售店|请确定一个格点|沿街道|则\s*[A-Za-z\\]+?\s*=|=$)"
)


def looks_like_next_question_contamination(lines: list[str], index: int) -> bool:
    text = lines[index].strip()
    if not text:
        return False
    if is_exam_section_heading(text):
        return True
    if re.match(r"^[一二三四五六七八九十]\s*[、.．。]+.*(?:填空|选择|解答)", text):
        return True
    if re.match(r"^[二三四]\s*[、.．。]+", text):
        return True
    window = " ".join(line.strip() for line in lines[index : index + 3] if line.strip())
    has_question_blank = "____" in window or "\\_\\_" in window
    has_choice_prompt = (
        "（）" in window
        or "( )" in window
        or "[答]" in window
        or bool(OPTION_MARKER_RE.search(window))
        or bool(re.search(r"[\(（]\s*[ABCD]\s*[\)）]", window))
    )
    has_prompt_text = bool(ANSWER_CONTAMINATION_PROMPT_RE.search(window))
    return bool(ANSWER_CONTAMINATION_START_RE.match(text) and (has_question_blank or has_choice_prompt or has_prompt_text))


def trim_objective_answer_contamination(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    kept: list[str] = []
    for index, line in enumerate(lines):
        if kept and looks_like_next_question_contamination(lines, index):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def parse_embedded_marked_answers(answer_questions: list[Question]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for question in answer_questions:
        text = question.normalized_text.strip()
        marker_positions = [(marker, text.find(marker)) for marker in ("【答案】", "【解答】") if text.find(marker) >= 0]
        if not marker_positions:
            continue
        marker, marker_index = min(marker_positions, key=lambda item: item[1])
        before = text[:marker_index].strip()
        after = text[marker_index + len(marker) :].strip()
        section_index = first_section_index(after)
        if section_index >= 0:
            answer_text = after[:section_index].strip()
            section_text = after[section_index:].strip()
        else:
            answer_text = after
            section_text = ""
        if not answer_text and before:
            answer_text = before.splitlines()[-1].strip(" 。.;；")
        analysis_text = ""
        analysis_match = re.search(r"【解析】(.*?)(?=【点评】|【分析】|【说明】|$)", section_text, flags=re.S)
        if analysis_match:
            analysis_text = analysis_match.group(1).strip()
        if question.question_no <= 18:
            analysis_text = trim_objective_answer_contamination(analysis_text)
        parts = [part for part in (answer_text, "【解析】" + analysis_text if analysis_text else "") if part]
        if parts:
            answers[question.question_no] = "\n".join(parts)
    return answers


INLINE_ANSWER_START_RE = re.compile(
    r"(?m)^\s*(\d{1,2})\s*[.．、]\s*(?=(?:[（(]\d{4}[）)]|设|若|已知|函数|计算|如图|在|从|为了|某|方程|不等式|点|直线|圆|椭圆|抛物线|数列|向量|集合|曲线|盒子|二项式|一个|为|有))"
)


def parse_inline_answer_blocks(answer_questions: list[Question]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for question in answer_questions:
        text = question.normalized_text.strip()
        matches = list(INLINE_ANSWER_START_RE.finditer(text))
        for index, match in enumerate(matches):
            question_no = int(match.group(1))
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            block = text[match.start() : end].strip()
            if "【解析】" not in block and "【答案】" not in block and "【解答】" not in block:
                continue
            block = re.sub(r"^\s*\d{1,2}\s*[.．、]\s*", "", block).strip()
            answers[question_no] = block
    return answers


def parse_unmarked_analysis_answers(answer_questions: list[Question]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for question in answer_questions:
        text = question.normalized_text.strip()
        if question.question_no <= 18 and "【解析】" in text:
            marker_positions = [
                index
                for marker in ("【思路分析】", "【解析】", "【解答】", "【分析】")
                if (index := text.find(marker)) >= 0
            ]
            start = min(marker_positions) if marker_positions else 0
            answers[question.question_no] = trim_objective_answer_contamination(text[start:].strip())
    return answers


def parse_compact_answer_keys(answer_questions: list[Question]) -> dict[int, str]:
    answers: dict[int, str] = {}
    numbered_answer_re = re.compile(r"^\s*(\d{1,2})\s*[、.．]\s*(.+?)\s*$")
    for question in answer_questions:
        if question.question_no > 18:
            continue
        text = question.normalized_text.strip()
        if "【答案】" in text or "【解答】" in text:
            continue
        if "解析" in text or len(text) > 240:
            continue
        if "；" not in text and ";" not in text:
            continue
        chunks = [chunk.strip(" \t\r\n。；;") for chunk in re.split(r"[；;]", text) if chunk.strip(" \t\r\n。；;")]
        if not chunks:
            continue
        first_match = numbered_answer_re.match(chunks[0])
        if not first_match and chunks[0]:
            answers[question.question_no] = chunks[0].strip()
        for chunk in chunks:
            match = numbered_answer_re.match(chunk)
            if match:
                answers[int(match.group(1))] = match.group(2).strip()
    return answers


PACKED_DIRECT_ANSWER_RE = re.compile(r"(?<![\dA-Za-z\\])(\d{1,2})\s*[.．、]\s*(?=\S)")
REFERENCE_ANSWER_HEADING_RE = re.compile(r"(参考答案|参考解答|答案)")
REFERENCE_NUMBERED_LINE_RE = re.compile(r"^\s*(\d{1,2})\s*(?:[.．、]|题[.．、。:：]?)\s*(.*)$")
SOLUTION_QUESTION_HEADING_RE = re.compile(r"^\s*[（(]\s*本题满分")


def looks_like_solution_question_heading(text: str) -> bool:
    stripped = text.strip()
    return bool(SOLUTION_QUESTION_HEADING_RE.match(stripped) or stripped.startswith("本题共有"))


def trim_solution_answer_contamination(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    kept: list[str] = []
    for line in lines:
        if kept and looks_like_solution_question_heading(line):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def parse_reference_answer_tables(answer_questions: list[Question]) -> dict[int, str]:
    text = "\n".join(question.normalized_text for question in answer_questions if question.normalized_text)
    marker_matches = list(REFERENCE_ANSWER_HEADING_RE.finditer(text))
    if not marker_matches:
        return {}
    answers: dict[int, str] = {}
    for marker_match in marker_matches:
        block = text[marker_match.end() :]
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        current_no: int | None = None
        current_parts: list[str] = []
        seen_numbered = False
        for line in lines:
            if current_no is not None and looks_like_solution_question_heading(line):
                if current_parts:
                    answer = "\n".join(current_parts).strip()
                    if current_no >= 19:
                        answer = trim_solution_answer_contamination(answer)
                    answers[current_no] = answer
                current_no = None
                current_parts = []
                seen_numbered = True
                continue
            match = REFERENCE_NUMBERED_LINE_RE.match(line)
            if match:
                question_no = int(match.group(1))
                if not 1 <= question_no <= 30:
                    continue
                if current_no is not None and current_parts:
                    answer = "\n".join(current_parts).strip()
                    if current_no >= 19:
                        answer = trim_solution_answer_contamination(answer)
                    answers[current_no] = answer
                current_no = question_no
                current_parts = [match.group(2).strip()] if match.group(2).strip() else []
                seen_numbered = True
                continue
            if current_no is not None:
                current_parts.append(line)
            elif seen_numbered:
                continue
        if current_no is not None and current_parts:
            answer = "\n".join(current_parts).strip()
            if current_no >= 19:
                answer = trim_solution_answer_contamination(answer)
            answers[current_no] = answer
    return answers


QUESTION_STEM_HINTS = (
    "已知",
    "若",
    "设",
    "函数",
    "如图",
    "求",
    "证明",
    "计算",
    "下列",
    "选择",
    "则",
    "为",
)


def looks_like_direct_objective_answer(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    first = lines[0].strip(" 。.;；")
    if not first:
        return False
    if "____" in first or "\\_\\_" in first:
        return False
    if re.fullmatch(r"[ABCD]", first):
        return True
    if len(first) <= 80 and not any(hint in first for hint in QUESTION_STEM_HINTS):
        return True
    return bool(re.fullmatch(r"\(?[ABCD]\)?\s*[，,;；]?.*", first) and len(first) <= 120)


def split_packed_direct_answers(question_no: int, text: str) -> dict[int, str]:
    stripped = text.strip()
    matches = [match for match in PACKED_DIRECT_ANSWER_RE.finditer(stripped) if match.start() > 0]
    if not matches:
        return {question_no: stripped}

    answers: dict[int, str] = {}
    first_answer = stripped[: matches[0].start()].strip(" 。.;；")
    if first_answer:
        answers[question_no] = first_answer
    for index, match in enumerate(matches):
        answer_no = int(match.group(1))
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(stripped)
        answer = stripped[match.end() : next_start].strip(" 。.;；")
        if question_no < answer_no <= 30 and answer:
            answers[answer_no] = answer
    return answers


def parse_direct_answer_fallbacks(answer_questions: list[Question]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for question in answer_questions:
        text = question.normalized_text.strip()
        if not text:
            continue
        has_analysis = "解析：" in text or "解析:" in text
        has_inline_final_phrase = "应填入" in text or "答案为" in text or "解集为" in text
        if question.question_no <= 16 and (looks_like_direct_objective_answer(text) or has_analysis or has_inline_final_phrase):
            answers.update(split_packed_direct_answers(question.question_no, text))
        elif question.question_no <= 18 and ("[答]" in text or "【答】" in text or ("解析" in text and OPTION_MARKER_RE.search(text))):
            answers[question.question_no] = text
        elif question.question_no >= 17 and looks_like_solution_answer_text(text):
            answers[question.question_no] = text
    return answers


def looks_like_solution_answer_text(text: str) -> bool:
    stripped = text.strip()
    return bool(
        stripped.startswith(("解：", "解:", "解(", "解（", "证明", "证：", "证:"))
        or stripped.startswith(("（1）", "(1)", "（Ⅰ）", "Ⅰ"))
        or "\n解：" in stripped
        or "[解]" in stripped
        or "【解】" in stripped
        or "\n证明" in stripped
    )


def parse_answers(answer_questions: list[Question]) -> dict[int, str]:
    structured_answers: dict[int, str] = {}
    structured_answers.update(parse_reference_answer_tables(answer_questions))
    for parser in (
        parse_embedded_marked_answers,
        parse_marked_answers,
        parse_inline_answer_blocks,
        parse_direct_answer_fallbacks,
        parse_compact_answer_keys,
    ):
        for question_no, answer in parser(answer_questions).items():
            structured_answers.setdefault(question_no, answer)
    for question_no, answer in parse_unmarked_analysis_answers(answer_questions).items():
        structured_answers.setdefault(question_no, answer)
    for question_no, answer in list(structured_answers.items()):
        if question_no <= 18:
            structured_answers[question_no] = trim_objective_answer_contamination(answer)
    if structured_answers:
        answers = dict(structured_answers)
        for question in answer_questions:
            if question.question_no >= 19 and question.question_no not in answers:
                text = question.normalized_text.strip()
                if looks_like_solution_answer_text(text):
                    answers[question.question_no] = text
        return answers

    answers: dict[int, str] = {}
    for question in answer_questions:
        text = question.normalized_text.strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            continue
        if question.question_no <= 16:
            answers[question.question_no] = lines[0]
        else:
            answers[question.question_no] = text
    return answers


STRUCTURED_DIRECT_ANSWER_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[.．、]\s*(.*?)(?=\s*(?<!\d)\d{1,2}\s*[.．、]\s*|$)", re.S)
STRUCTURED_ITEM_START_RE = re.compile(r"^\s*(\d{1,2})\s*[.．、]\s*(.*)$", re.S)
STRUCTURED_EMBEDDED_ITEM_RE = re.compile(r"[;；]\s*(\d{1,2})\s*[.．、]\s*")
STRUCTURED_SOLUTION_START_RE = re.compile(r"^\s*(1[7-9]|2[0-1])\s*[.．、]\s*(.*)$")


def clean_structured_answer(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \t\r\n;；。")


def text_from_mineru_content_parts(parts: list[dict[str, Any]]) -> str:
    output: list[str] = []
    for part in parts:
        part_type = part.get("type")
        content = str(part.get("content") or "")
        if not content:
            continue
        if part_type in {"equation_inline", "equation_block"}:
            output.append(f"${content}$")
        else:
            output.append(content)
    return "".join(output)


def parse_structured_answer_items(items: list[str]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for item in items:
        match = STRUCTURED_ITEM_START_RE.match(item)
        if not match:
            continue
        question_no = int(match.group(1))
        remainder = match.group(2)
        embedded = list(STRUCTURED_EMBEDDED_ITEM_RE.finditer(remainder))
        first_end = embedded[0].start() if embedded else len(remainder)
        first_answer = clean_structured_answer(remainder[:first_end])
        if 1 <= question_no <= 16 and first_answer:
            answers[question_no] = first_answer
        for index, embedded_match in enumerate(embedded):
            embedded_no = int(embedded_match.group(1))
            end = embedded[index + 1].start() if index + 1 < len(embedded) else len(remainder)
            embedded_answer = clean_structured_answer(remainder[embedded_match.end() : end])
            if 1 <= embedded_no <= 16 and embedded_answer:
                answers[embedded_no] = embedded_answer
    return answers


def parse_structured_answer_table(table_html: str) -> dict[int, str]:
    cells = [html.unescape(re.sub(r"<.*?>", "", cell)).strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", table_html, flags=re.S)]
    if len(cells) < 4 or "题号" not in cells[0]:
        return {}
    try:
        split_index = cells.index("代号")
    except ValueError:
        return {}
    question_numbers = cells[1:split_index]
    answers = cells[split_index + 1 :]
    result: dict[int, str] = {}
    for question_no_text, answer in zip(question_numbers, answers):
        try:
            question_no = int(question_no_text)
        except ValueError:
            continue
        answer = clean_structured_answer(answer)
        if 1 <= question_no <= 16 and answer:
            result[question_no] = answer
    return result


def parse_structured_solution_answers(blocks: list[dict[str, Any]]) -> dict[int, str]:
    answers: dict[int, str] = {}
    current_no: int | None = None
    current_parts: list[str] = []
    skipped_types = {"header", "footer", "page_number", "aside_text", "page_footnote"}
    for block in blocks:
        if block.get("type") in skipped_types:
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        match = STRUCTURED_SOLUTION_START_RE.match(text)
        if match:
            if current_no is not None and current_parts:
                answers[current_no] = trim_solution_answer_contamination("\n".join(current_parts).strip())
            current_no = int(match.group(1))
            current_parts = [text]
        elif current_no is not None:
            current_parts.append(text)
    if current_no is not None and current_parts:
        answers[current_no] = trim_solution_answer_contamination("\n".join(current_parts).strip())
    return answers


def parse_mineru_structured_answers(extracted: dict[str, Any]) -> dict[int, str]:
    answers: dict[int, str] = {}
    blocks = extracted.get("blocks", [])
    direct_answer_section = False
    table_answer_section = False
    for block in blocks:
        item = block.get("mineru_item") or {}
        item_type = item.get("type") or block.get("type")
        block_text = str(block.get("text") or "")
        if re.search(r"第\s*1\s*题至\s*12\s*题", block_text):
            direct_answer_section = True
            table_answer_section = False
            continue
        if re.search(r"第\s*13\s*题至\s*16\s*题", block_text):
            direct_answer_section = False
            table_answer_section = True
            continue
        if re.search(r"第\s*17\s*题至\s*21\s*题", block_text):
            direct_answer_section = False
            table_answer_section = False
            continue

        if item_type == "list" and direct_answer_section:
            list_items = item.get("list_items") or []
            if list_items:
                answers.update(parse_structured_answer_items([str(value) for value in list_items]))
                continue
            content = item.get("content") or {}
            structured_items: list[str] = []
            for list_item in content.get("list_items", []) if isinstance(content, dict) else []:
                parts = list_item.get("item_content") or []
                if isinstance(parts, list):
                    structured_items.append(text_from_mineru_content_parts(parts))
            answers.update(parse_structured_answer_items(structured_items))
        elif item_type == "table" and table_answer_section:
            table_body = str(item.get("table_body") or item.get("content") or block.get("text") or "")
            answers.update(parse_structured_answer_table(table_body))
    answers.update(parse_structured_solution_answers(blocks))
    return {question_no: answer for question_no, answer in sorted(answers.items()) if 1 <= question_no <= 30 and answer}


def flag_private_unicode(text: str) -> bool:
    return any("\ue000" <= ch <= "\uf8ff" for ch in text)


def validate_questions(questions: list[Question], answers: dict[int, str]) -> dict[str, Any]:
    by_no: dict[int, list[Question]] = {}
    for q in questions:
        by_no.setdefault(q.question_no, []).append(q)

    max_seen_no = max(by_no) if by_no else 21
    expected_max = max(21, max_seen_no)
    expected = list(range(1, expected_max + 1))
    missing = [no for no in expected if no not in by_no]
    duplicates = [no for no, items in by_no.items() if len(items) > 1]
    warnings: list[str] = []

    for q in questions:
        flags: list[str] = []
        if q.question_no in duplicates:
            flags.append("duplicate_question_no")
        if flag_private_unicode(q.raw_text):
            flags.append("private_unicode_math_glyphs")
        if len(q.normalized_text) < 8:
            flags.append("very_short_question")
        if q.question_type == "single_choice" and len(q.options) < 4:
            flags.append("choice_options_incomplete")
        if q.question_no <= 16 and q.question_no not in answers:
            flags.append("answer_not_matched")
        if q.question_no >= 17 and len(q.normalized_text) < 60:
            flags.append("solution_question_too_short")
        if re.search(r"https?://|NO COMMERC|REPLiX", q.raw_text, re.IGNORECASE):
            flags.append("watermark_leaked")
        if q.figures and q.question_no not in {12, 15, 18, 19, 20, 21}:
            flags.append("figure_assignment_needs_review")
        q.flags = flags
        q.answer_raw = answers.get(q.question_no)
        base = 1.0
        base -= 0.12 * len(flags)
        if q.question_no <= 16 and q.answer_raw:
            base += 0.05
        q.confidence = round(max(0.05, min(0.98, base)), 2)
        warnings.extend(flags)

    return {
        "question_count": len(questions),
        "expected_count": expected_max,
        "missing_question_numbers": missing,
        "duplicate_question_numbers": duplicates,
        "flag_counts": {flag: warnings.count(flag) for flag in sorted(set(warnings))},
        "low_confidence_question_numbers": [q.question_no for q in questions if q.confidence < 0.75],
    }


def profile_dataset(data_dir: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for pdf in sorted(data_dir.glob("*.pdf")):
        page_count, text_len, image_count = read_pdf_text_len(pdf)
        rows.append(
            {
                "name": pdf.name,
                "pages": page_count,
                "text_len": text_len,
                "image_count": image_count,
                "kind": "blank"
                if BLANK_TOKEN in pdf.name
                else "answer"
                if ANSWER_TOKEN in pdf.name
                else "solution"
                if SOLUTION_TOKEN in pdf.name
                else "unknown",
            }
        )
    text_lengths = [row["text_len"] for row in rows]
    image_counts = [row["image_count"] for row in rows]
    return {
        "pdf_count": len(rows),
        "blank_count": sum(1 for row in rows if row["kind"] == "blank"),
        "answer_or_solution_count": sum(1 for row in rows if row["kind"] in {"answer", "solution"}),
        "text_len_min": min(text_lengths) if text_lengths else 0,
        "text_len_median": int(statistics.median(text_lengths)) if text_lengths else 0,
        "image_count_median": int(statistics.median(image_counts)) if image_counts else 0,
        "rows": rows,
    }


def make_run_id(pdf: Path) -> str:
    digest = hashlib.sha1(str(pdf).encode("utf-8")).hexdigest()[:8]
    year_match = re.search(r"(20\d{2})", pdf.name)
    year = year_match.group(1) if year_match else "paper"
    return f"{year}_{digest}"


def render_review_html(path: Path, run_dir: Path, questions: list[Question], figures: list[Figure], validation: dict[str, Any]) -> None:
    fig_by_id = {fig.figure_id: fig for fig in figures}
    cards: list[str] = []
    for q in sorted(questions, key=lambda item: item.question_no):
        flags = " ".join(f"<span class='flag'>{html.escape(flag)}</span>" for flag in q.flags) or "<span class='ok'>ok</span>"
        options = ""
        if q.options:
            options = "<ul class='options'>" + "".join(
                f"<li><b>{html.escape(label)}</b>. {html.escape(text)}</li>" for label, text in sorted(q.options.items())
            ) + "</ul>"
        fig_html = ""
        for fig_id in q.figures:
            fig = fig_by_id.get(fig_id)
            if fig and fig.path:
                candidate = Path(fig.path)
                if (run_dir / candidate).exists():
                    src_path = candidate
                elif (run_dir / "paper" / candidate).exists():
                    src_path = Path("paper") / candidate
                else:
                    src_path = candidate
                src = html.escape(src_path.as_posix())
                fig_html += f"<img src='{src}' alt='{html.escape(fig_id)}'>"
        answer = html.escape(q.answer_raw or "")
        cards.append(
            f"""
            <article class="question" id="q{q.question_no}">
              <header>
                <h2>Q{q.question_no}</h2>
                <span>{html.escape(q.question_type)}</span>
                <span>confidence {q.confidence:.2f}</span>
              </header>
              <div class="flags">{flags}</div>
              <h3>Normalized</h3>
              <pre>{html.escape(q.normalized_text)}</pre>
              {options}
              <h3>Matched Answer</h3>
              <pre>{answer}</pre>
              <div class="figures">{fig_html}</div>
            </article>
            """
        )

    page_imgs = []
    pages_dir = run_dir / "paper" / "pages"
    for image in sorted(pages_dir.glob("page_*.png")):
        rel = image.relative_to(run_dir).as_posix()
        page_imgs.append(f"<img src='{html.escape(rel)}' alt='{html.escape(image.stem)}'>")

    summary = html.escape(json.dumps(validation, ensure_ascii=False, indent=2))
    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Exam Review Prototype</title>
  <style>
    :root {{
      color-scheme: light;
      --border: #d9dde5;
      --text: #1d2433;
      --muted: #657084;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --warn: #b54708;
      --ok: #177245;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      color: var(--text);
      background: var(--bg);
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(320px, 42vw) 1fr;
      min-height: 100vh;
    }}
    aside {{
      height: 100vh;
      overflow: auto;
      border-right: 1px solid var(--border);
      background: #eef1f5;
      padding: 12px;
    }}
    main {{
      height: 100vh;
      overflow: auto;
      padding: 16px 18px 32px;
    }}
    img {{
      display: block;
      max-width: 100%;
      border: 1px solid var(--border);
      background: white;
      margin-bottom: 12px;
    }}
    .summary, .question {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 14px;
    }}
    header {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
      margin-bottom: 10px;
    }}
    h1, h2, h3 {{ margin: 0; }}
    h1 {{ font-size: 20px; margin-bottom: 10px; }}
    h2 {{ font-size: 18px; }}
    h3 {{ font-size: 13px; color: var(--muted); margin: 10px 0 6px; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      padding: 8px;
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 6px;
      line-height: 1.45;
      font-family: Consolas, "Microsoft YaHei", monospace;
      font-size: 13px;
    }}
    .flag, .ok {{
      display: inline-block;
      padding: 2px 6px;
      border-radius: 999px;
      font-size: 12px;
      margin: 0 4px 4px 0;
    }}
    .flag {{ color: var(--warn); background: #fff3e8; border: 1px solid #ffd7ad; }}
    .ok {{ color: var(--ok); background: #e8f6ee; border: 1px solid #bfe5cf; }}
    .options {{ margin: 8px 0; padding-left: 24px; }}
    .figures {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 8px;
      margin-top: 10px;
    }}
    .figures img {{ margin: 0; }}
    @media (max-width: 900px) {{
      .layout {{ grid-template-columns: 1fr; }}
      aside {{ height: 44vh; border-right: 0; border-bottom: 1px solid var(--border); }}
      main {{ height: auto; }}
    }}
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <h1>Source Pages</h1>
      {''.join(page_imgs)}
    </aside>
    <main>
      <section class="summary">
        <h1>Run Summary</h1>
        <pre>{summary}</pre>
      </section>
      {''.join(cards)}
    </main>
  </div>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")


def write_analysis_report(
    path: Path,
    dataset_profile: dict[str, Any],
    selected_pair: dict[str, Any],
    validation: dict[str, Any],
    questions: list[Question],
    figures: list[Figure],
) -> None:
    flags = validation["flag_counts"]
    low = validation["low_confidence_question_numbers"]
    missing = validation["missing_question_numbers"]
    content = f"""# Prototype Run Analysis

Generated: {datetime.now().isoformat(timespec="seconds")}

## Dataset

- PDFs found: {dataset_profile['pdf_count']}
- Blank papers: {dataset_profile['blank_count']}
- Answer/solution papers: {dataset_profile['answer_or_solution_count']}
- Median text length: {dataset_profile['text_len_median']}
- Median embedded image count: {dataset_profile['image_count_median']}

## Selected Pair

- Paper: `{selected_pair['blank']}`
- Answer-like file: `{selected_pair.get('answer_like')}`

## Pipeline Result

- Parsed questions: {validation['question_count']} / {validation['expected_count']}
- Missing question numbers: {missing or 'none'}
- Duplicate question numbers: {validation['duplicate_question_numbers'] or 'none'}
- Cropped figures: {len(figures)}
- Low-confidence questions: {low or 'none'}
- Flag counts: `{json.dumps(flags, ensure_ascii=False)}`

## Observed Problems

1. PDF text extraction already has formula fragmentation. Fractions, vectors, sets, and roots often become many short lines, so a lightweight LLM should receive source spans and rendered page crops, not plain OCR text only.
2. Private-use math glyphs appear in the extracted text. These need model normalization or a font/glyph mapping pass before LaTeX conversion.
3. Figure assignment is only geometry-based in this prototype. Page 2 option diagrams and oil-pot geometry can be attached to nearby questions, but this is not reliable enough for production without manual rebinding.
4. Answer parsing works for simple first-line answers, but solution files mix answer, derivation, and scoring text. A separate answer-schema extractor is needed.
5. Watermark/header/footer removal must be source-specific. The local PDFs contain repeated archive banners that can leak into OCR blocks.

## Suggested Modifications

1. Insert a `document_blocks` table before the question bank. Store every text/image/formula/table block with page, bbox, reading order, extractor name, and raw payload.
2. Replace direct text-to-question conversion with a two-stage parser: deterministic question-boundary detection first, then LLM JSON normalization per question with strict schema validation.
3. Add a glyph repair stage before LaTeX normalization. Track unmapped private-use characters and fail the question into manual review if any remain.
4. Treat answer matching as its own module. Extract answer candidates by question number, answer type, and explanation span; then attach to questions with confidence.
5. Store figure candidates separately from bound figures. The review UI should let humans bind, unbind, split, and recrop figures.
6. Add automatic checks that block publishing: missing question number, incomplete choice options, unmatched answer for objective questions, private-use glyphs, leaked watermark, and invalid LaTeX render.
7. Keep the LLM audit as advisory. It should write flags and suggested patches, while human verification controls the final `published` state.

## Question Flags

"""
    for q in sorted(questions, key=lambda item: item.question_no):
        content += f"- Q{q.question_no}: type={q.question_type}, confidence={q.confidence}, flags={q.flags or ['ok']}\n"
    path.write_text(content, encoding="utf-8")


def run_pipeline(args: argparse.Namespace) -> Path:
    data_dir = Path(args.data_dir)
    output_dir = ensure_dir(Path(args.output_dir))
    pairs = discover_pairs(data_dir)
    if args.list_pairs:
        write_json(output_dir / "pairs.json", pairs)
        print(f"Wrote {output_dir / 'pairs.json'}")
        return output_dir / "pairs.json"

    if args.paper:
        selected = {"blank": args.paper, "answer_like": args.answer}
    else:
        selected = select_pair(pairs, args.year)

    paper_pdf = Path(selected["blank"])
    answer_pdf = Path(selected["answer_like"]) if selected.get("answer_like") else None
    run_id = args.run_id or make_run_id(paper_pdf)
    run_dir = ensure_dir(output_dir / "runs" / run_id)

    dataset_profile = profile_dataset(data_dir)
    write_json(run_dir / "dataset_profile.json", dataset_profile)
    write_json(run_dir / "selected_pair.json", selected)

    paper_data_id = sanitize_data_id(f"{run_id}_paper")
    answer_data_id = sanitize_data_id(f"{run_id}_answer")
    if args.extractor == "mineru_vlm":
        paper_extracted = extract_document_mineru_vlm(paper_pdf, ensure_dir(run_dir / "paper"), args, paper_data_id)
    else:
        paper_extracted = extract_document_local(paper_pdf, ensure_dir(run_dir / "paper"), dpi=args.dpi)
    extraction_manifest: dict[str, Any] = {
        "paper_extractor": paper_extracted.get("extractor", args.extractor),
        "answer_extractor": None,
        "answer_fallback": None,
    }
    questions = segment_questions(paper_extracted)
    figures = assign_figures(questions, paper_extracted)

    answer_questions: list[Question] = []
    answers: dict[int, str] = {}
    if answer_pdf and answer_pdf.exists():
        if args.extractor == "mineru_vlm":
            fallback = getattr(args, "answer_fallback_extractor", "none")
            answer_dir = ensure_dir(run_dir / "answer")
            fallback_reason: str | None = None
            cached_result_path = answer_dir / "mineru_result.json"
            if fallback == "local" and cached_result_path.exists() and not mineru_extract_has_content_list(answer_dir / "mineru_extract"):
                cached_payload = read_json(cached_result_path)
                if not mineru_result_is_done(cached_payload, answer_pdf.name):
                    result_items = cached_payload.get("data", {}).get("extract_result", [])
                    item = result_items[0] if result_items else {}
                    fallback_reason = f"cached MinerU state={item.get('state')}, err={item.get('err_msg')}"
            try:
                if fallback_reason:
                    raise RuntimeError(fallback_reason)
                answer_extracted = extract_document_mineru_vlm(answer_pdf, answer_dir, args, answer_data_id)
            except Exception as exc:
                if fallback != "local":
                    raise
                fallback_dir = answer_dir
                write_json(
                    fallback_dir / "answer_fallback.json",
                    {
                        "from_extractor": "mineru_vlm",
                        "fallback_extractor": "local_pymupdf",
                        "reason": str(exc),
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    },
                )
                answer_extracted = extract_document_local(answer_pdf, fallback_dir, dpi=args.dpi)
                extraction_manifest["answer_fallback"] = {
                    "from_extractor": "mineru_vlm",
                    "fallback_extractor": "local_pymupdf",
                    "reason": str(exc),
                }
        else:
            answer_extracted = extract_document_local(answer_pdf, ensure_dir(run_dir / "answer"), dpi=args.dpi)
        extraction_manifest["answer_extractor"] = answer_extracted.get("extractor", args.extractor)
        answer_questions = segment_questions(answer_extracted, answer_mode=True)
        answers = parse_answers(answer_questions)
        mineru_structured_answers = parse_mineru_structured_answers(answer_extracted)
        if mineru_structured_answers:
            write_json(run_dir / "answers_by_question_mineru_structured.json", mineru_structured_answers)
            answers.update(mineru_structured_answers)
        write_json(run_dir / "answers_raw.json", [asdict(q) for q in answer_questions])
        write_json(run_dir / "answers_by_question.json", answers)
    write_json(run_dir / "extraction_manifest.json", extraction_manifest)

    validation = validate_questions(questions, answers)
    write_json(run_dir / "questions_normalized.json", [asdict(q) for q in sorted(questions, key=lambda item: item.question_no)])
    write_json(run_dir / "figures_assigned.json", [asdict(fig) for fig in figures])
    write_json(run_dir / "validation_report.json", validation)
    render_review_html(run_dir / "review.html", run_dir, questions, figures, validation)
    write_analysis_report(run_dir / "analysis_report.md", dataset_profile, selected, validation, questions, figures)

    print(f"Run directory: {run_dir}")
    print(f"Parsed questions: {validation['question_count']} / {validation['expected_count']}")
    print(f"Missing: {validation['missing_question_numbers']}")
    print(f"Flags: {validation['flag_counts']}")
    print(f"Review HTML: {run_dir / 'review.html'}")
    print(f"Analysis: {run_dir / 'analysis_report.md'}")
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prototype math-exam-to-question-bank pipeline.")
    parser.add_argument("--data-dir", default="gaokao_papers", help="Directory containing local PDF dataset.")
    parser.add_argument("--output-dir", default="code/output", help="Directory for generated pipeline artifacts.")
    parser.add_argument("--extractor", choices=["local", "mineru_vlm"], default="local", help="Document extraction backend.")
    parser.add_argument("--year", type=int, default=2026, help="Year to select when --paper is not provided.")
    parser.add_argument("--paper", help="Explicit blank paper PDF path.")
    parser.add_argument("--answer", help="Explicit answer/solution PDF path.")
    parser.add_argument("--run-id", help="Optional stable run id.")
    parser.add_argument("--dpi", type=int, default=144, help="Render DPI for page and figure crops.")
    parser.add_argument("--list-pairs", action="store_true", help="Only write discovered paper pairs.")
    parser.add_argument("--mineru-token-file", default=".mineru_token", help="Path to MinerU API token file.")
    parser.add_argument("--mineru-language", default="ch", help="MinerU document language.")
    parser.add_argument("--mineru-ocr", action=argparse.BooleanOptionalAction, default=True, help="Enable MinerU OCR.")
    parser.add_argument("--enable-formula", action=argparse.BooleanOptionalAction, default=True, help="Enable MinerU formula recognition.")
    parser.add_argument("--enable-table", action=argparse.BooleanOptionalAction, default=True, help="Enable MinerU table recognition.")
    parser.add_argument("--page-ranges", help="MinerU page range string, e.g. 1-4 or 2,4-6 for precise API.")
    parser.add_argument("--extra-formats", nargs="*", default=[], choices=["docx", "html", "latex"], help="Optional MinerU export formats.")
    parser.add_argument("--mineru-timeout", type=int, default=900, help="MinerU polling timeout in seconds.")
    parser.add_argument("--mineru-poll-interval", type=int, default=10, help="MinerU polling interval in seconds.")
    parser.add_argument(
        "--answer-fallback-extractor",
        choices=["none", "local"],
        default="none",
        help="Fallback extractor for answer PDFs when MinerU VLM fails.",
    )
    return parser


if __name__ == "__main__":
    run_pipeline(build_parser().parse_args())
