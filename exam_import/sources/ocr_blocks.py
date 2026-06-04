from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from typing import Any

from exam_import.core.io import read_json, write_json


QUESTION_PREFIX_RE = re.compile(r"^\s*(\d{1,3})\s*[\.．、]")
OPTION_PREFIX_RE = re.compile(r"^\s*[\(（][A-DＡ-Ｄ][\)）]")


@dataclass(frozen=True)
class PageScale:
    width: int
    height: int
    coord_width: float = 1000.0
    coord_height: float = 1000.0

    def scale_bbox(self, bbox: list[float]) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = [float(value) for value in bbox]
        return (
            int(round(x1 / self.coord_width * self.width)),
            int(round(y1 / self.coord_height * self.height)),
            int(round(x2 / self.coord_width * self.width)),
            int(round(y2 / self.coord_height * self.height)),
        )


def load_ocr_payload(part_dir: Path) -> dict[str, Any]:
    return read_json(part_dir / "ocr_blocks.json")


def layout_items_for_part(part_dir: Path) -> list[dict[str, Any]]:
    ocr = load_ocr_payload(part_dir)
    items = _text_unit_items(ocr)
    if not items:
        items = _text_block_items(ocr)
    items.extend(_figure_block_items(ocr))
    return sorted(items, key=_block_order)


def build_packets(
    *,
    run_id: str,
    part_dir: Path,
    out_run_dir: Path,
    blocks: list[dict[str, Any]],
    namespace: str,
    packet_dir_name: str,
) -> list[dict[str, Any]]:
    packet_dir = out_run_dir / packet_dir_name
    if packet_dir.exists():
        shutil.rmtree(packet_dir)
    packet_dir.mkdir(parents=True, exist_ok=True)
    packets: list[dict[str, Any]] = []
    for page in _page_numbers(blocks):
        labels = _label_page_blocks_namespaced(blocks, page, namespace)
        packet = _compact_page_packet_namespaced(
            run_id=run_id,
            part_dir=part_dir,
            out_run_dir=out_run_dir,
            page=page,
            namespace=namespace,
            blocks=blocks,
            labels=labels,
        )
        write_json(packet_dir / f"page_{page:03d}.json", packet)
        packets.append(packet)
    return packets


def load_packets(packet_dir: Path) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for path in sorted(packet_dir.glob("page_*.json")):
        payload = read_json(path)
        if isinstance(payload, dict):
            packets.append(payload)
    return packets


def text_excerpt(text: str, limit: int = 1000) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _text_unit_items(ocr: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, unit in enumerate(ocr.get("text_units") or [], start=1):
        order = int(unit.get("order") or index)
        items.append(
            {
                "block_id": unit.get("unit_id"),
                "type": "text_unit",
                "kind": "text",
                "bbox": unit.get("bbox"),
                "text": unit.get("text"),
                "page": unit.get("page"),
                "source_block_id": unit.get("block_id"),
                "order": order,
                "reading_order": order,
                "order_source": "mineru_content_list" if ocr.get("extractor") == "mineru_vlm" else "ocr_text_units",
            }
        )
    return items


def _text_block_items(ocr: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, block in enumerate(ocr.get("blocks") or [], start=1):
        if block.get("figure_id"):
            continue
        order = int(block.get("reading_order") or block.get("order") or index)
        items.append(
            {
                "block_id": block.get("block_id"),
                "type": block.get("type") or "text",
                "kind": "text",
                "bbox": block.get("bbox"),
                "text": block.get("text"),
                "page": block.get("page"),
                "order": order,
                "reading_order": order,
                "order_source": block.get("order_source") or "ocr_blocks",
            }
        )
    return items


def _figure_block_items(ocr: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, block in enumerate(ocr.get("blocks") or [], start=1):
        if not _is_visual_block(block):
            continue
        block_type = str(block.get("type") or "").lower()
        kind = "table" if "table" in block_type else "chart" if "chart" in block_type else "image"
        order = int(block.get("reading_order") or block.get("order") or index)
        items.append(
            {
                "block_id": block.get("block_id"),
                "figure_id": block.get("figure_id"),
                "type": block.get("type") or kind,
                "kind": kind,
                "bbox": block.get("bbox"),
                "text": block.get("text") or "",
                "page": block.get("page"),
                "order": order,
                "reading_order": order,
                "order_source": block.get("order_source") or "ocr_blocks",
            }
        )
    return items


def _is_visual_block(block: dict[str, Any]) -> bool:
    block_type = str(block.get("type") or "").lower()
    if block.get("figure_id"):
        return True
    return any(token in block_type for token in ("image", "table", "chart"))


def _page_numbers(blocks: list[dict[str, Any]]) -> list[int]:
    return sorted({int(item.get("page") or 0) for item in blocks if int(item.get("page") or 0) > 0})


def _block_order(block: dict[str, Any]) -> tuple[int, int, float, float, str]:
    bbox = block.get("bbox") or [0, 0, 0, 0]
    return (
        int(block.get("page") or 0),
        int(block.get("reading_order") or block.get("order") or 0),
        float(bbox[1]) if isinstance(bbox, list) and len(bbox) >= 2 else 0.0,
        float(bbox[0]) if isinstance(bbox, list) and len(bbox) >= 1 else 0.0,
        str(block.get("block_id") or ""),
    )


def _label_page_blocks(blocks: list[dict[str, Any]], page: int) -> dict[str, str]:
    labels: dict[str, str] = {}
    b_count = 0
    p_count = 0
    for block in sorted((item for item in blocks if int(item.get("page") or 0) == page), key=_block_order):
        if _is_visual_block(block):
            p_count += 1
            local = f"P{p_count:02d}"
        else:
            b_count += 1
            local = f"B{b_count:02d}"
        block_id = str(block.get("block_id") or "")
        figure_id = str(block.get("figure_id") or "")
        if block_id:
            labels[block_id] = local
        if figure_id:
            labels[figure_id] = local
    return labels


def _namespaced_label(namespace: str, page: int, local_label: str) -> str:
    return f"{namespace}-V{page:02d}-{local_label}"


def _label_page_blocks_namespaced(blocks: list[dict[str, Any]], page: int, namespace: str) -> dict[str, str]:
    local = _label_page_blocks(blocks, page)
    return {key: _namespaced_label(namespace, page, value) for key, value in local.items()}


def _compact_page_packet_namespaced(
    *,
    run_id: str,
    part_dir: Path,
    out_run_dir: Path,
    page: int,
    namespace: str,
    blocks: list[dict[str, Any]],
    labels: dict[str, str],
) -> dict[str, Any]:
    annotated = _annotate_page_namespaced(
        part_dir=part_dir,
        out_run_dir=out_run_dir,
        page=page,
        namespace=namespace,
        blocks=blocks,
        labels=labels,
    )
    page_image = (part_dir / "pages" / f"page_{page:03d}.png").resolve()
    local_labels = _label_page_blocks(blocks, page)
    page_blocks: list[dict[str, Any]] = []
    for block in sorted((item for item in blocks if int(item.get("page") or 0) == page), key=_block_order):
        text = str(block.get("text") or "")
        detected = QUESTION_PREFIX_RE.match(text)
        global_label = labels.get(str(block.get("figure_id") or block.get("block_id")), "")
        if not global_label:
            continue
        local_label = local_labels.get(str(block.get("figure_id") or block.get("block_id")), global_label.rsplit("-", 1)[-1])
        kind = block.get("kind") or "text"
        page_blocks.append(
            {
                "label": global_label,
                "global_label": global_label,
                "local_label": local_label,
                "block_id": block.get("block_id"),
                "type": block.get("type") or kind,
                "kind": kind,
                "bbox": block.get("bbox"),
                "order": block.get("reading_order") or block.get("order") or 0,
                "reading_order": block.get("reading_order") or block.get("order") or 0,
                "order_source": block.get("order_source"),
                "figure_id": block.get("figure_id"),
                "detected_question_prefix": int(detected.group(1)) if detected else None,
                "looks_like_option": bool(OPTION_PREFIX_RE.match(text)),
                "text": text if kind == "table" else text_excerpt(text, limit=1000),
            }
        )
    return {
        "run_id": run_id,
        "namespace": namespace,
        "page": page,
        "page_image": str(page_image),
        "annotated_page_image": str(annotated.resolve()),
        "blocks": page_blocks,
    }


def _annotate_page_namespaced(
    *,
    part_dir: Path,
    out_run_dir: Path,
    page: int,
    namespace: str,
    blocks: list[dict[str, Any]],
    labels: dict[str, str],
) -> Path:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is required to render Step2 annotated pages") from exc

    src = part_dir / "pages" / f"page_{page:03d}.png"
    if not src.exists():
        raise FileNotFoundError(f"Missing page image: {src}")
    image = Image.open(src).convert("RGB")
    scale = PageScale(width=image.width, height=image.height)
    draw = ImageDraw.Draw(image, "RGBA")
    for block in sorted((item for item in blocks if int(item.get("page") or 0) == page), key=_block_order):
        bbox = block.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        label = labels.get(str(block.get("figure_id") or block.get("block_id")), "")
        if not label:
            continue
        local = label.rsplit("-", 1)[-1]
        color = (214, 82, 51) if local.startswith("P") else (42, 104, 173)
        _draw_labeled_box(draw, scale, bbox, label, color, width=3 if local.startswith("P") else 2)
    out = out_run_dir / "annotated_pages" / f"{namespace.lower()}_page_{page:03d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


def _draw_labeled_box(
    draw: Any,
    scale: PageScale,
    bbox: list[float],
    label: str,
    color: tuple[int, int, int],
    width: int = 2,
) -> None:
    x1, y1, x2, y2 = scale.scale_bbox(bbox)
    draw.rectangle((x1, y1, x2, y2), outline=color, width=width)
    text_bbox = draw.textbbox((x1, y1), label)
    pad = 2
    label_h = text_bbox[3] - text_bbox[1]
    label_w = text_bbox[2] - text_bbox[0]
    draw.rectangle((x1, max(0, y1 - label_h - 2 * pad), x1 + label_w + 2 * pad, y1), fill=color)
    draw.text((x1 + pad, max(0, y1 - label_h - pad)), label, fill=(255, 255, 255))
