from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

WORKTREE_ROOT = Path(__file__).resolve().parent
LLM_IMAGE_DATA_URI_MAX_BYTES = 9_500_000
CODE_ROOT = WORKTREE_ROOT.parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from common_blocks import (  # noqa: E402
    asset_kind,
    block_by_label,
    label_to_a,
    label_to_q,
    labels_in_range,
    labels_in_range_with_shared_start,
    rule_visual_assets,
    unique_str,
)
from common_io import data_uri, read_json, text_excerpt, write_json  # noqa: E402
from common_llm import call_chat_structured_json, configure_llm_provider_env  # noqa: E402
from common_step2_crops import question_surface_labels_from_full_range  # noqa: E402
from common_token_budget import DEFAULT_OUTPUT_RESERVE_TOKENS, configure_token_budget_env  # noqa: E402

DEFAULT_MODEL = "Qwen/Qwen3.6-35B-A3B"
DEFAULT_LLM_PROVIDER = "siliconflow"
DEFAULT_OUTPUT_ROOT = WORKTREE_ROOT / "runs_step2_exam_blocks"

RANGE_DETECTOR_TOOL_SCHEMA = {
    "type": "object",
    "required": ["question_ranges", "noise_blocks", "risks"],
    "properties": {
        "question_ranges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question_no", "start_label", "end_label", "visual_labels", "confidence", "reason"],
                "properties": {
                    "question_no": {"type": "integer"},
                    "start_label": {"type": "string"},
                    "end_label": {"type": "string"},
                    "visual_labels": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "noise_blocks": {"type": "array", "items": {"type": "string"}},
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "question_no", "block_labels", "severity", "evidence"],
                "properties": {
                    "type": {"type": "string", "enum": ["boundary", "missing", "uncertain"]},
                    "question_no": {"type": "integer"},
                    "block_labels": {"type": "array", "items": {"type": "string"}},
                    "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                    "evidence": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def normalize_worker_models(primary_model: str, worker_models: list[str] | None) -> list[str]:
    models = [str(model).strip() for model in (worker_models or []) if str(model).strip()]
    if not models:
        models = [primary_model]
    return list(dict.fromkeys(models))


def multi_model_name(worker_models: list[str]) -> str:
    if len(worker_models) == 1:
        return worker_models[0]
    return "multi_model[" + ",".join(worker_models) + "]"

QUESTION_PREFIX_RE = re.compile(r"^\s*(\d{1,2})\s*[\.．、]")
OPTION_PREFIX_RE = re.compile(r"^\s*[\(（][A-DＡ-Ｄ][\)）]")
NOISE_RE = re.compile(
    r"考生注意|本大题|填空题|选择题|解答题|答题纸|试卷|公众号|准考证|姓名[:：]?|学校[:：]|班级[:：]|座位号[:：]|"
    r"第\s*\d*\s*页\s*(?:共\s*\d+\s*页)?|保留版权|"
    r"^\s*[一二三四五六七八九十]+、|^202\d|^数学\s*$"
)


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


def page_image_path(source_run_dir: Path, page: int) -> Path:
    return source_run_dir / "paper" / "pages" / f"page_{page:03d}.png"


def block_text(block: dict[str, Any]) -> str:
    if block.get("text"):
        return str(block.get("text") or "")
    item = block.get("mineru_item") or {}
    return str(item.get("content") or item.get("text") or "")


ORDER_SENTINEL = 10**12


def block_reading_order(block: dict[str, Any]) -> float | None:
    for key in ("reading_order", "order", "mineru_order"):
        value = block.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def block_order(block: dict[str, Any]) -> tuple[int, int, float, float, float, str]:
    bbox = block.get("bbox") or [0, 0, 0, 0]
    reading_order = block_reading_order(block)
    has_order = 0 if reading_order is not None else 1
    return (
        int(block.get("page") or 0),
        has_order,
        reading_order if reading_order is not None else ORDER_SENTINEL,
        float(bbox[1]),
        float(bbox[0]),
        str(block.get("block_id") or ""),
    )


def normalize_raw_bbox(bbox: list[float] | None, page_meta: dict[str, Any]) -> list[float] | None:
    if not bbox:
        return None
    width = float(page_meta.get("source_width_pt") or page_meta.get("width_pt") or 1000.0)
    height = float(page_meta.get("source_height_pt") or page_meta.get("height_pt") or 1000.0)
    x1, y1, x2, y2 = [float(value) for value in bbox]
    return [round(x1 / width * 1000, 1), round(y1 / height * 1000, 1), round(x2 / width * 1000, 1), round(y2 / height * 1000, 1)]


def span_content(span: dict[str, Any]) -> str:
    if span.get("content") is not None:
        content = str(span.get("content") or "")
        if span.get("type") == "inline_equation":
            return f"${content}$"
        if span.get("type") == "interline_equation":
            return f"$${content}$$"
        return content
    if span.get("html") is not None:
        return str(span.get("html") or "")
    return ""


def raw_layout_text_units(ocr: dict[str, Any], source_run_dir: Path) -> list[dict[str, Any]]:
    layout_path = source_run_dir / "paper" / "mineru_extract" / "layout.json"
    layout = read_json(layout_path, {})
    page_meta_by_page = {int(page.get("page") or idx + 1): page for idx, page in enumerate(ocr.get("pages") or [])}
    units: list[dict[str, Any]] = []
    order = 0

    def leaf_nodes(node: dict[str, Any], top_index: int) -> list[dict[str, Any]]:
        children = node.get("blocks") or []
        if children:
            leaves: list[dict[str, Any]] = []
            for child in children:
                leaves.extend(leaf_nodes(child, top_index))
            return leaves
        return [node | {"_top_index": top_index}]

    for page_idx, page in enumerate(layout.get("pdf_info") or [], start=1):
        page_meta = page_meta_by_page.get(page_idx, {})
        for top in page.get("preproc_blocks") or []:
            top_index = int(top.get("index") or 0)
            for leaf in leaf_nodes(top, top_index):
                if leaf.get("type") in {"image_body", "chart_body"}:
                    continue
                lines = leaf.get("lines") or []
                if not lines:
                    continue
                for line_idx, line in enumerate(lines, start=1):
                    text = "".join(span_content(span) for span in line.get("spans") or [])
                    text = re.sub(r"\s+", " ", text).strip()
                    if not text:
                        continue
                    order += 1
                    raw_bbox = line.get("bbox") or leaf.get("bbox")
                    units.append(
                        {
                            "block_id": f"raw_p{page_idx:03d}_t{top_index:04d}_l{line_idx:03d}_{order:05d}",
                            "type": f"raw_{leaf.get('type') or top.get('type') or 'text'}",
                            "bbox": normalize_raw_bbox(raw_bbox, page_meta),
                            "raw_bbox": raw_bbox,
                            "text": text,
                            "page": page_idx,
                            "source_top_index": top_index,
                            "source_type": top.get("type"),
                            "leaf_type": leaf.get("type"),
                            "order": order,
                            "reading_order": order,
                            "order_source": "mineru_layout",
                        }
                    )
    return units


def text_unit_items(ocr: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, unit in enumerate(ocr.get("text_units") or [], start=1):
        order = unit.get("order") or index
        items.append(
            {
                "block_id": unit.get("unit_id"),
                "type": "text_unit",
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


def figure_block_items(ocr: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, block in enumerate(ocr.get("blocks") or [], start=1):
        if not block.get("figure_id"):
            continue
        item = dict(block)
        order = item.get("order") or index
        item.setdefault("order", order)
        item.setdefault("reading_order", order)
        item.setdefault("order_source", "mineru_content_list" if ocr.get("extractor") == "mineru_vlm" else "ocr_blocks")
        items.append(item)
    return items


def layout_items(ocr: dict[str, Any], granularity: str, source_run_dir: Path) -> list[dict[str, Any]]:
    if granularity == "blocks":
        return list(ocr.get("blocks") or [])
    if granularity == "raw_units":
        items = raw_layout_text_units(ocr, source_run_dir)
        items.extend(figure_block_items(ocr))
        return items
    items = text_unit_items(ocr)
    if not items:
        items = raw_layout_text_units(ocr, source_run_dir)
    items.extend(figure_block_items(ocr))
    return items


def label_page_blocks(blocks: list[dict[str, Any]], page: int) -> dict[str, str]:
    labels: dict[str, str] = {}
    b_count = 0
    p_count = 0
    for block in sorted((item for item in blocks if int(item.get("page") or 0) == page), key=block_order):
        figure_id = str(block.get("figure_id") or "")
        if figure_id:
            p_count += 1
            label = f"P{p_count:02d}"
            labels[str(block.get("block_id"))] = label
            labels[figure_id] = label
        else:
            b_count += 1
            labels[str(block.get("block_id"))] = f"B{b_count:02d}"
    return labels


def draw_labeled_box(draw: ImageDraw.ImageDraw, scale: PageScale, bbox: list[float], label: str, color: tuple[int, int, int], width: int = 2) -> None:
    x1, y1, x2, y2 = scale.scale_bbox(bbox)
    draw.rectangle((x1, y1, x2, y2), outline=color, width=width)
    text_bbox = draw.textbbox((x1, y1), label)
    pad = 2
    label_h = text_bbox[3] - text_bbox[1]
    label_w = text_bbox[2] - text_bbox[0]
    draw.rectangle((x1, max(0, y1 - label_h - 2 * pad), x1 + label_w + 2 * pad, y1), fill=color)
    draw.text((x1 + pad, max(0, y1 - label_h - pad)), label, fill=(255, 255, 255))


def annotate_page(source_run_dir: Path, out_run_dir: Path, page: int, blocks: list[dict[str, Any]], labels: dict[str, str]) -> Path:
    src = page_image_path(source_run_dir, page)
    if not src.exists():
        raise FileNotFoundError(f"missing page image: {src}")
    image = Image.open(src).convert("RGB")
    scale = PageScale(width=image.width, height=image.height)
    draw = ImageDraw.Draw(image, "RGBA")
    for block in sorted((item for item in blocks if int(item.get("page") or 0) == page), key=block_order):
        if not block.get("bbox"):
            continue
        label = labels.get(str(block.get("figure_id") or block.get("block_id")), "")
        if not label:
            continue
        color = (214, 82, 51) if label.startswith("P") else (42, 104, 173)
        draw_labeled_box(draw, scale, block["bbox"], label, color, width=3 if label.startswith("P") else 2)
    out = out_run_dir / "annotated_pages" / f"page_{page:03d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


def compact_page_packet(run_id: str, granularity: str, source_run_dir: Path, out_run_dir: Path, page: int, blocks: list[dict[str, Any]], labels: dict[str, str]) -> dict[str, Any]:
    annotated = annotate_page(source_run_dir, out_run_dir, page, blocks, labels)
    page_blocks: list[dict[str, Any]] = []
    for block in sorted((item for item in blocks if int(item.get("page") or 0) == page), key=block_order):
        text = block_text(block)
        q_prefix = QUESTION_PREFIX_RE.match(text)
        label = labels.get(str(block.get("figure_id") or block.get("block_id")), "")
        page_blocks.append(
            {
                "label": label,
                "block_id": block.get("block_id"),
                "type": block.get("type"),
                "bbox": block.get("bbox"),
                "order": block.get("reading_order") or block.get("order"),
                "order_source": block.get("order_source"),
                "figure_id": block.get("figure_id"),
                "detected_question_prefix": int(q_prefix.group(1)) if q_prefix else None,
                "looks_like_option": bool(OPTION_PREFIX_RE.match(text)),
                "text": text_excerpt(text),
            }
        )
    return {
        "run_id": run_id,
        "granularity": granularity,
        "page": page,
        "page_image": str(page_image_path(source_run_dir, page)),
        "annotated_page_image": str(annotated),
        "blocks": page_blocks,
    }



ANSWER_KEYWORDS = [
    "\u7b54\u6848\u8981\u70b9",
    "\u53c2\u8003\u7b54\u6848",
    "\u7b54\u6848\u4e0e\u89e3\u6790",
    "\u8bc4\u5206\u6807\u51c6",
]
ANSWER_SHEET_KEYWORDS = [
    "\u8d34\u6761\u5f62\u7801",
    "\u8d34\u6761\u5f62\u7801\u533a",
    "\u8bf7\u5728\u5404\u9898\u76ee\u7684\u7b54\u9898\u533a\u5185\u4f5c\u7b54",
    "\u8d85\u51fa\u9ed1\u8272\u77e9\u5f62\u8fb9\u6846",
    "\u9009\u62e9\u9898\u6d82\u5199\u533a",
]
COMMENTARY_KEYWORDS = [
    "\u4e13\u5bb6\u70b9\u8bc4",
    "\u7acb\u5b66\u79d1\u4e4b\u57fa",
    "\u8bd5\u9898\u5b88\u6b63\u51fa\u65b0",
]
INTERLEAVED_SOLUTION_KEYWORDS = [
    "\u53c2\u8003\u7b54\u6848\u4e0e\u8bd5\u9898\u89e3\u6790",
    "\u8bd5\u9898\u89e3\u6790",
    "\u3010\u601d\u8def\u5206\u6790",
    "\u3010\u89e3\u6790",
    "\u3010\u5f52\u7eb3\u603b\u7ed3",
]
INTERLEAVED_SOLUTION_START_RE = re.compile(
    r"^\s*(?:"
    r"【\s*(?:思路分析|解析|解答|答案|归纳总结|评分标准|点评)\s*】"
    r"|(?:思路分析|解析|解答|答案|归纳总结|评分标准|点评)\s*[：:]"
    r")"
)
TOP_LEVEL_QUESTION_LINE_RE = re.compile(r"(?m)^\s*\d{1,2}\s*[.、．]")

CAPTION_TEXT_RE = re.compile(
    r"^\s*[\(\[\u3010\uff08]?\s*"
    r"(?:"
    r"\u7b2c\s*(?P<question_no>\d{1,3})\s*\u9898\s*(?P<question_kind>[\u56fe\u8868])"
    r"|(?P<asset_kind>[\u56fe\u8868])\s*(?P<asset_no>\d{1,3}(?:[-\u2013\u2014]\d{1,3})?)"
    r")"
    r"\s*[\)\]\u3011\uff09\uff1a:.,，。]*\s*$"
)


OPTION_CAPTION_RE = re.compile(r"^\s*[\(\[\uff08]?\s*([A-D])\s*[.\uff0e\u3001\)\]\uff09]?\s*$", re.IGNORECASE)



def namespaced_label(namespace: str, page: int, local_label: str) -> str:
    return f"{namespace}-V{page:02d}-{local_label}"


def label_page_blocks_namespaced(blocks: list[dict[str, Any]], page: int, namespace: str) -> dict[str, str]:
    local = label_page_blocks(blocks, page)
    return {key: namespaced_label(namespace, page, label) for key, label in local.items()}


def annotate_page_namespaced(
    source_doc_dir: Path,
    out_run_dir: Path,
    page: int,
    namespace: str,
    blocks: list[dict[str, Any]],
    labels: dict[str, str],
) -> Path:
    src = source_doc_dir / "pages" / f"page_{page:03d}.png"
    if not src.exists():
        raise FileNotFoundError(f"missing page image: {src}")
    image = Image.open(src).convert("RGB")
    scale = PageScale(width=image.width, height=image.height)
    draw = ImageDraw.Draw(image, "RGBA")
    for block in sorted((item for item in blocks if int(item.get("page") or 0) == page), key=block_order):
        if not block.get("bbox"):
            continue
        label = labels.get(str(block.get("figure_id") or block.get("block_id")), "")
        if not label:
            continue
        local = label.rsplit("-", 1)[-1]
        color = (214, 82, 51) if local.startswith("P") else (42, 104, 173)
        draw_labeled_box(draw, scale, block["bbox"], label, color, width=3 if local.startswith("P") else 2)
    out = out_run_dir / "annotated_pages" / f"{namespace.lower()}_page_{page:03d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


def compact_page_packet_namespaced(
    run_id: str,
    source_run_dir: Path,
    out_run_dir: Path,
    page: int,
    namespace: str,
    blocks: list[dict[str, Any]],
    labels: dict[str, str],
    include_text: bool = True,
) -> dict[str, Any]:
    annotated = annotate_page_namespaced(source_run_dir, out_run_dir, page, namespace, blocks, labels)
    local_labels = label_page_blocks(blocks, page)
    page_blocks: list[dict[str, Any]] = []
    for block in sorted((item for item in blocks if int(item.get("page") or 0) == page), key=block_order):
        text = block_text(block)
        q_prefix = QUESTION_PREFIX_RE.match(text)
        block_key = str(block.get("figure_id") or block.get("block_id"))
        label = labels.get(block_key, "")
        row = {
            "label": label,
            "global_label": label,
            "local_label": local_labels.get(block_key, ""),
            "block_id": block.get("block_id"),
            "type": block.get("type"),
            "bbox": block.get("bbox"),
            "order": block.get("reading_order") or block.get("order"),
            "order_source": block.get("order_source"),
            "figure_id": block.get("figure_id"),
        }
        if include_text:
            row.update(
                {
                    "detected_question_prefix": int(q_prefix.group(1)) if q_prefix else None,
                    "looks_like_option": bool(OPTION_PREFIX_RE.match(text)),
                    "text": text_excerpt(text, limit=260),
                }
            )
        page_blocks.append(row)
    return {
        "run_id": run_id,
        "namespace": namespace,
        "page": page,
        "page_image": str(source_run_dir / "pages" / f"page_{page:03d}.png"),
        "annotated_page_image": str(annotated),
        "blocks": page_blocks,
    }


def page_text_from_ocr(ocr: dict[str, Any], page: int) -> str:
    parts: list[str] = []
    for block in ocr.get("blocks") or []:
        if int(block.get("page") or 0) != page:
            continue
        text = str(block.get("text") or block.get("content") or "")
        if text:
            parts.append(text)
    return "\n".join(parts)


def compact_layout_block(block: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"label": str(block.get("label") or "")}
    block_type = str(block.get("type") or "").strip()
    if block_type:
        row["type"] = "text" if "text" in block_type.lower() else block_type
    text = str(block.get("text") or "").strip()
    if text:
        row["text"] = text
    return row


def compact_answer_layout_packet(
    run_id: str,
    mode: str,
    answer_packets: list[dict[str, Any]],
    known_question_numbers: list[int],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "mode": mode,
        "known_question_numbers": known_question_numbers,
        "answer_pages": [
            {
                "page": packet["page"],
                "blocks": [
                    compact_layout_block(block)
                    for block in packet.get("blocks") or []
                    if isinstance(block, dict) and block.get("label")
                ],
            }
            for packet in answer_packets
        ],
    }


def has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def normalize_doc_raw_bbox(
    bbox: list[float] | None,
    page_meta: dict[str, Any],
    layout_page: dict[str, Any],
) -> list[float] | None:
    meta = dict(page_meta or {})
    page_size = layout_page.get("page_size")
    if isinstance(page_size, list) and len(page_size) >= 2:
        meta.setdefault("source_width_pt", page_size[0])
        meta.setdefault("source_height_pt", page_size[1])
    return normalize_raw_bbox(bbox, meta)


def raw_layout_text_units_for_doc(ocr: dict[str, Any], doc_dir: Path) -> list[dict[str, Any]]:
    layout = read_json(doc_dir / "mineru_extract" / "layout.json", {})
    page_meta_by_page = {int(page.get("page") or idx + 1): page for idx, page in enumerate(ocr.get("pages") or [])}
    units: list[dict[str, Any]] = []
    order = 0

    def leaf_nodes(node: dict[str, Any], top_index: int) -> list[dict[str, Any]]:
        children = node.get("blocks") or []
        if children:
            leaves: list[dict[str, Any]] = []
            for child in children:
                leaves.extend(leaf_nodes(child, top_index))
            return leaves
        return [node | {"_top_index": top_index}]

    for page_idx, page in enumerate(layout.get("pdf_info") or [], start=1):
        page_meta = page_meta_by_page.get(page_idx, {})
        for top in page.get("preproc_blocks") or []:
            top_index = int(top.get("index") or 0)
            for leaf in leaf_nodes(top, top_index):
                if leaf.get("type") in {"image_body", "chart_body"}:
                    continue
                lines = leaf.get("lines") or []
                if not lines:
                    continue
                for line_idx, line in enumerate(lines, start=1):
                    text = "".join(span_content(span) for span in line.get("spans") or [])
                    text = re.sub(r"\s+", " ", text).strip()
                    if not text:
                        continue
                    order += 1
                    raw_bbox = line.get("bbox") or leaf.get("bbox")
                    units.append(
                        {
                            "block_id": f"raw_p{page_idx:03d}_t{top_index:04d}_l{line_idx:03d}_{order:05d}",
                            "type": f"raw_{leaf.get('type') or top.get('type') or 'text'}",
                            "bbox": normalize_doc_raw_bbox(raw_bbox, page_meta, page),
                            "raw_bbox": raw_bbox,
                            "text": text,
                            "page": page_idx,
                            "source_top_index": top_index,
                            "source_type": top.get("type"),
                            "leaf_type": leaf.get("type"),
                            "order": order,
                            "reading_order": order,
                            "order_source": "mineru_layout",
                        }
                    )
    return units


def layout_items_for_doc(ocr: dict[str, Any], doc_dir: Path) -> list[dict[str, Any]]:
    # MinerU content_list is already in model reading order; prefer its text_units
    # over geometry-sorted raw lines when available.
    items = text_unit_items(ocr) if ocr.get("extractor") == "mineru_vlm" else []
    if not items:
        items = raw_layout_text_units_for_doc(ocr, doc_dir)
    if not items:
        items = text_unit_items(ocr)
    items.extend(figure_block_items(ocr))
    return items


def is_answer_sheet_page(text: str) -> bool:
    return has_any(text, ANSWER_SHEET_KEYWORDS)


def looks_like_interleaved_solution(page_texts: dict[int, str], pages: list[int], first_answer_page: int | None) -> bool:
    if not pages or first_answer_page != pages[0]:
        return False
    sample = "\n".join(page_texts.get(page, "") for page in pages[: min(2, len(pages))])
    question_hits = len(TOP_LEVEL_QUESTION_LINE_RE.findall(sample))
    marker_hits = sum(sample.count(keyword) for keyword in INTERLEAVED_SOLUTION_KEYWORDS)
    return question_hits >= 2 and marker_hits >= 2


def classify_pages(ocr: dict[str, Any]) -> dict[str, Any]:
    pages = sorted({int(block.get("page") or 0) for block in ocr.get("blocks") or []})
    page_texts = {page: page_text_from_ocr(ocr, page) for page in pages}
    first_answer_page: int | None = None
    for page in pages:
        text = page_texts[page]
        if has_any(text, ANSWER_KEYWORDS) and not is_answer_sheet_page(text):
            first_answer_page = page
            break

    if looks_like_interleaved_solution(page_texts, pages, first_answer_page):
        active_pages = [
            page
            for page in pages
            if not is_answer_sheet_page(page_texts[page]) and not has_any(page_texts[page], COMMENTARY_KEYWORDS)
        ]
        roles = {
            page: (
                "answer_sheet"
                if is_answer_sheet_page(page_texts[page])
                else "commentary"
                if has_any(page_texts[page], COMMENTARY_KEYWORDS)
                else "interleaved_solution"
            )
            for page in pages
        }
        return {
            "mode": "interleaved_solution",
            "pages": pages,
            "first_answer_page": first_answer_page,
            "question_pages": active_pages,
            "answer_pages": active_pages,
            "page_roles": {str(page): role for page, role in roles.items()},
        }

    roles: dict[int, str] = {}
    for page in pages:
        text = page_texts[page]
        if is_answer_sheet_page(text):
            roles[page] = "answer_sheet"
        elif has_any(text, COMMENTARY_KEYWORDS):
            roles[page] = "commentary"
        elif first_answer_page is not None and page >= first_answer_page:
            roles[page] = "answer"
        else:
            roles[page] = "question"

    answer_pages = [page for page, role in roles.items() if role == "answer"]
    question_pages = [page for page, role in roles.items() if role == "question"]
    mode = "paper_then_answers" if answer_pages else "paper_only"
    return {
        "mode": mode,
        "pages": pages,
        "first_answer_page": first_answer_page,
        "question_pages": question_pages,
        "answer_pages": answer_pages,
        "page_roles": {str(page): role for page, role in roles.items()},
    }

def compact_block(block: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str(block.get("text") or "")).strip()
    row = {
        "label": block.get("label"),
        "type": block.get("type"),
    }
    if "text" in block:
        row["text"] = text_excerpt(text, limit=300)
    return row


def compact_packet(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "page": packet.get("page"),
        "blocks": [compact_block(block) for block in packet.get("blocks") or [] if block.get("label")],
    }


def page_numbers(blocks: list[dict[str, Any]]) -> list[int]:
    return sorted({int(block.get("page") or 0) for block in blocks if int(block.get("page") or 0) > 0})


def build_packets(
    run_id: str,
    doc_dir: Path,
    out_run_dir: Path,
    blocks: list[dict[str, Any]],
    namespace: str,
    packet_dir_name: str,
    include_text: bool = True,
) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for page in page_numbers(blocks):
        labels = label_page_blocks_namespaced(blocks, page, namespace)
        packet = compact_page_packet_namespaced(run_id, doc_dir, out_run_dir, page, namespace, blocks, labels, include_text=include_text)
        packet_dir = out_run_dir / packet_dir_name
        write_json(packet_dir / f"page_{page:03d}.json", packet)
        packets.append(packet)
    return packets


def build_range_messages(
    packets: list[dict[str, Any]],
    run_id: str,
    stream: str,
    mode: str,
    structured_output: str,
) -> list[dict[str, Any]]:
    compact = {
        "run_id": run_id,
        "stream": stream,
        "mode": mode,
        "pages": [compact_packet(packet) for packet in packets],
    }
    mode_rule = {
        "pure_paper": "当前输入是试题页。每个范围是一道完整顶层题，包含题干、选项、图表和续写块。",
        "pure_answer": "当前输入是答案页或解析页。每个范围是一道题完整的答案、解析、解答过程或评分标准。",
        "mixed": "当前输入中题干和答案解析交错出现。每个范围是一道完整顶层题，包含题干、选项、图表、答案、解析、评分标准和续写块。",
    }[mode]
    if structured_output == "tool_calling":
        output_rule = (
            "重要输出约束：\n"
            "- 必须调用工具 submit_step2_ranges 提交结果。\n"
            "- 普通回复正文必须为空；不得在 message.content 中写 JSON、Markdown、代码块、解释或分析过程。\n"
            "- 工具参数根键必须是 question_ranges。\n"
            "- 不要返回或复述包含 run_id、pages、blocks 等根键的输入包。\n"
            "- 在答案页模式下，如果一个答案表包含多个题号的答案，必须每个题号输出一个范围；必要时可复用同一个起始标签和结束标签。\n\n"
            "工具参数格式示例：\n"
        )
        final_rule = "\n\n现在调用工具 submit_step2_ranges。普通正文保持为空。"
        system_prompt = (
            "你是试卷 OCR 顶层题号块范围检测器。本次任务必须通过调用工具 "
            "submit_step2_ranges 完成，不得在普通回复正文中输出结果。"
        )
    else:
        output_rule = (
            "重要输出约束：\n"
            "- JSON 根键必须是 question_ranges。\n"
            "- 不要返回或复述包含 run_id、pages、blocks 等根键的输入包。\n"
            "- 在答案页模式下，如果一个答案表包含多个题号的答案，必须每个题号输出一个范围；必要时可复用同一个起始标签和结束标签。\n\n"
            "只返回严格 JSON，格式如下：\n"
        )
        final_rule = "\n\n现在只返回根键为 question_ranges 的范围 JSON 对象。"
        system_prompt = "只检测试卷 OCR 顶层题号的起止块范围。返回严格 JSON。"
    example_namespace = "M" if mode == "mixed" else "A" if stream == "answer" else "Q"
    example_start = f"{example_namespace}-V01-B01"
    example_end = f"{example_namespace}-V01-B03"
    example_visual = f"{example_namespace}-V01-P01"
    example_block = f"{example_namespace}-V01-B02"
    instruction = (
        "/no_think\n"
        "你是试卷 OCR 版面块范围检测器，只识别顶层题号对应的块范围。\n"
        f"{mode_rule}\n\n"
        "任务：\n"
        "- 对每个可见的顶层题号，输出起始标签和结束标签。\n"
        "- 不要在范围检测阶段区分题干块和答案块。\n"
        "- 不要分类图、表或图片；如果某个图、表或图片明显属于该题但因跨栏、页首浮动等版面原因不在 start_label 到 end_label 的连续范围内，只把它的标签填入 visual_labels。\n"
        "- 不得解题、改写、推断缺失题目或修正 OCR。\n"
        "- 顶层题号形如 1.、2.、21.；(1)(2) 这类小问不是新题。\n"
        "- 21-I、21-II、21-Ⅰ、21-Ⅱ 这类带连字符或罗马数字的编号是同一顶层题号的分部，不是新的顶层题；必须合并到题号 21 的同一个范围。\n"
        "- 选择题范围必须从包含该题题号和题干的 OCR 块开始，不能从 (A)、(B)、(C)、(D) 选项块开始；选项属于同一题，但不是题目的起始边界。\n"
        "- 在答案页或解析页中，每道题的答案、解析、解答过程、评分标准，从该题第一个可见题号或解答标记开始，连续抄到下一道顶层题号出现之前为止；下一题题号及其后内容是排他边界，不得归入上一题。\n"
        "- 相邻题目的范围允许重叠，尤其是题号块、选项块或跨栏版面边界不确定时，可以让前后两题共用边界标签；但 reason 中必须说明这是边界上下文重叠。\n"
        "- 在答案页模式下，页首或正文中的紧凑横排答案键也必须按题号拆分；例如“1. ... 2. ... 3. ...”、“10. ... 11. ...”或“二、15. B 16. C”均不是噪声。每个可见顶层题号都要输出一个 question_range；如果多个题号位于同一个 OCR 块，允许这些题复用同一个 start_label 和 end_label，并在 reason 中说明该块包含紧凑答案键。\n"
        "- 如果一个 OCR 块同时包含上一题尾部和下一题开头，允许相邻两个范围共用该块，作为上一题的结束标签和下一题的起始标签。\n"
        "- 如果一道题跨页续写，且后续页面没有新的顶层题号，则结束标签延伸到最后一个明显属于该题的续写块。\n"
        "- 忽略页眉、页脚、大题标题、页码、二维码、水印和装饰块。\n\n"
        + output_rule
        + json.dumps(
            {
                "question_ranges": [
                    {
                        "question_no": 1,
                        "start_label": example_start,
                        "end_label": example_end,
                        "visual_labels": [example_visual],
                        "confidence": 0.0,
                        "reason": "简短理由",
                    }
                ],
                "noise_blocks": [example_start],
                "risks": [
                    {
                        "type": "boundary|missing|uncertain",
                        "question_no": 1,
                        "block_labels": [example_block],
                        "severity": "info|warning|error",
                        "evidence": "简短证据",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n\n"
        "紧凑版面包：\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
        + final_rule
    )
    content: list[dict[str, Any]] = []
    for packet in packets:
        image_path = Path(str(packet.get("annotated_page_image") or ""))
        if not image_path.is_absolute():
            image_path = WORKTREE_ROOT / image_path
        content.append({"type": "text", "text": f"第 {packet['page']} 页标注图："})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": data_uri(
                        image_path,
                        max_bytes=LLM_IMAGE_DATA_URI_MAX_BYTES,
                        cache_dir=image_path.parent / "image_data_uri_cache",
                    )
                },
            }
        )
    content.append({"type": "text", "text": instruction})
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def call_range_detector(
    packets: list[dict[str, Any]],
    run_id: str,
    stream: str,
    mode: str,
    model: str,
    timeout: int,
    out_path: Path,
    force: bool,
    structured_output: str,
    enable_thinking: bool,
) -> dict[str, Any]:
    if out_path.exists() and not force:
        cached = read_json(out_path, {})
        if cached.get("question_ranges"):
            return cached
        raise ValueError(f"Cached Step2 range file has no question_ranges: {out_path}")
    compact = {
        "run_id": run_id,
        "stream": stream,
        "mode": mode,
        "pages": [compact_packet(packet) for packet in packets],
    }
    write_json(out_path.with_name(out_path.stem + "_input_compact.json"), compact)
    messages = build_range_messages(packets, run_id, stream, mode, structured_output)
    parsed, raw, elapsed = call_chat_structured_json(
        messages,
        model=model,
        timeout=timeout,
        structured_output=structured_output,
        tool_name="submit_step2_ranges",
        tool_description="提交 Step2 顶层题号块范围检测结果。",
        tool_schema=RANGE_DETECTOR_TOOL_SCHEMA,
        enable_thinking=enable_thinking,
    )
    normalized = normalize_ranges(parsed, packets)
    if not normalized.get("question_ranges"):
        write_json(out_path.with_name(out_path.stem + "_raw_empty.json"), {"raw_response": raw, "normalized": normalized})
        raise ValueError(f"Step2 range detector returned no question_ranges for {run_id} {stream} {mode}")
    normalized["raw_response"] = raw
    normalized["structured_output"] = structured_output
    normalized["llm_elapsed_seconds"] = round(elapsed, 3)
    normalized["llm_input_compact_bytes"] = len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    write_json(out_path, normalized)
    return normalized


def normalize_ranges(payload: dict[str, Any], packets: list[dict[str, Any]]) -> dict[str, Any]:
    blocks_by_label = block_by_label(packets)
    labels: set[str] = set(blocks_by_label)
    ranges: list[dict[str, Any]] = []
    for item in payload.get("question_ranges") or []:
        if not isinstance(item, dict):
            continue
        try:
            qno = int(item.get("question_no"))
        except (TypeError, ValueError):
            continue
        start = str(item.get("start_label") or "")
        end = str(item.get("end_label") or "")
        visual_labels = unique_str(
            [
                str(label)
                for label in item.get("visual_labels") or []
                if str(label) in labels and asset_kind(blocks_by_label.get(str(label), {}))
            ]
        )
        ranges.append(
            {
                "question_no": qno,
                "start_label": start,
                "end_label": end,
                "visual_labels": visual_labels,
                "confidence": item.get("confidence"),
                "reason": item.get("reason") or "",
                "valid": start in labels and end in labels,
            }
        )
    ranges.sort(key=lambda item: (int(item["start_label"].split("-V", 1)[1].split("-", 1)[0]), item["start_label"]))
    label_order: dict[str, int] = {}
    for packet in sorted(packets, key=lambda item: int(item.get("page") or 0)):
        for block in packet.get("blocks") or []:
            label = str(block.get("label") or "")
            if label and label not in label_order:
                label_order[label] = len(label_order)
    merged: dict[int, dict[str, Any]] = {}
    duplicate_count = 0
    for item in ranges:
        qno = int(item["question_no"])
        existing = merged.get(qno)
        if existing is None:
            merged[qno] = dict(item)
            continue
        duplicate_count += 1
        if label_order.get(str(item["start_label"]), 10**9) < label_order.get(str(existing["start_label"]), 10**9):
            existing["start_label"] = item["start_label"]
        if label_order.get(str(item["end_label"]), -1) > label_order.get(str(existing["end_label"]), -1):
            existing["end_label"] = item["end_label"]
        existing["visual_labels"] = unique_str((existing.get("visual_labels") or []) + (item.get("visual_labels") or []))
        try:
            existing["confidence"] = min(float(existing.get("confidence") or 0.0), float(item.get("confidence") or 0.0))
        except (TypeError, ValueError):
            existing["confidence"] = existing.get("confidence")
        reasons = [str(existing.get("reason") or ""), str(item.get("reason") or "")]
        existing["reason"] = "；".join(dict.fromkeys(reason for reason in reasons if reason))
        existing["valid"] = bool(existing.get("valid")) and bool(item.get("valid"))
    ranges = sorted(
        merged.values(),
        key=lambda item: (label_order.get(str(item.get("start_label") or ""), 10**9), int(item["question_no"])),
    )
    nums = [int(item["question_no"]) for item in ranges]
    missing = [num for num in range(min(nums), max(nums) + 1) if num not in nums] if nums else []
    result = {
        "question_ranges": ranges,
        "noise_blocks": [str(label) for label in payload.get("noise_blocks") or []],
        "risks": payload.get("risks") if isinstance(payload.get("risks"), list) else [],
        "missing_question_numbers_within_detected_span": missing,
    }
    if duplicate_count:
        result["merged_duplicate_question_range_count"] = duplicate_count
    return result


def labels_for_range(range_item: dict[str, Any], packets: list[dict[str, Any]]) -> list[str]:
    return unique_str(labels_in_range(range_item, packets) + [str(label) for label in range_item.get("visual_labels") or []])


def labels_for_shared_start_range(range_item: dict[str, Any], packets: list[dict[str, Any]]) -> list[str]:
    return unique_str(labels_in_range_with_shared_start(range_item, packets) + [str(label) for label in range_item.get("visual_labels") or []])


def packet_label_order(packets: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for packet in sorted(packets, key=lambda item: int(item.get("page") or 0)):
        for block in packet.get("blocks") or []:
            label = str(block.get("label") or "")
            if label:
                labels.append(label)
    return unique_str(labels)


def expand_labels_with_adjacent_context(labels: list[str], packets: list[dict[str, Any]], before: int = 1, after: int = 1) -> list[str]:
    ordered = packet_label_order(packets)
    if not labels or not ordered:
        return labels
    blocks = block_by_label(packets)
    selected_for_bounds = [
        label
        for label in labels
        if label in ordered and not asset_kind(blocks.get(label) or {})
    ]
    if not selected_for_bounds:
        selected_for_bounds = [label for label in labels if label in ordered]
    selected = [ordered.index(label) for label in selected_for_bounds]
    if not selected:
        return labels
    start = max(0, min(selected) - before)
    end = min(len(ordered) - 1, max(selected) + after)
    asset_labels = [
        label
        for label in labels
        if asset_kind(blocks.get(label) or {})
    ]
    return unique_str(ordered[start : end + 1] + asset_labels + [label for label in labels if label not in ordered])


def labels_by_page_position(labels: list[str], blocks_by_label: dict[str, dict[str, Any]]) -> list[str]:
    def key(label: str) -> tuple[int, float, float, str]:
        block = blocks_by_label.get(label) or {}
        bbox = block.get("bbox") if isinstance(block.get("bbox"), list) else [0, 0, 0, 0]
        return (
            int(block.get("page") or 0),
            float(bbox[1] if len(bbox) > 1 else 0),
            float(bbox[0] if len(bbox) > 0 else 0),
            label,
        )

    return sorted(unique_str(labels), key=key)


def build_alignment(
    run_id: str,
    pipeline_mode: str,
    question_ranges: dict[str, Any] | None,
    answer_ranges: dict[str, Any] | None,
    mixed_ranges: dict[str, Any] | None,
    question_packets: list[dict[str, Any]],
    answer_packets: list[dict[str, Any]],
    out_run_dir: Path,
) -> dict[str, Any]:
    question_blocks = block_by_label(question_packets)
    answer_blocks = block_by_label(answer_packets)
    question_groups: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    if pipeline_mode == "paper_plus_answer_file":
        answer_range_by_qno = {
            int(item["question_no"]): labels_for_range(item, answer_packets)
            for item in (answer_ranges or {}).get("question_ranges") or []
            if item.get("valid", True)
        }
        for item in (question_ranges or {}).get("question_ranges") or []:
            qno = int(item["question_no"])
            q_core_labels = labels_for_range(item, question_packets)
            q_labels = expand_labels_with_adjacent_context(q_core_labels, question_packets)
            a_labels = answer_range_by_qno.get(qno, [])
            q_surface_labels = question_surface_labels_from_full_range(labels_by_page_position(q_core_labels, question_blocks), question_blocks)
            assets = rule_visual_assets(q_core_labels, question_blocks)
            question_groups.append(
                {
                    "question_no": qno,
                    "block_labels": q_labels,
                    "core_block_labels": q_core_labels,
                    "question_surface_labels": q_surface_labels,
                    "visual_assets": assets,
                    "source": "v11_range_detector",
                }
            )
            rows.append(
                {
                    "question_no": qno,
                    "question_status": "found" if q_labels else "missing",
                    "answer_status": "found" if a_labels else "missing",
                    "question_labels": q_labels,
                    "question_core_labels": q_core_labels,
                    "question_surface_labels": q_surface_labels,
                    "visual_assets": [],
                    "answer_items": [
                        {
                            "question_no": qno,
                            "role": "answer",
                            "block_labels": a_labels,
                            "span_text_excerpt": "",
                            "continues_previous": False,
                            "confidence": 1.0,
                            "reason": "v11 pure_answer start/end range matched by question_no.",
                        }
                    ]
                    if a_labels
                    else [],
                }
            )

    elif pipeline_mode == "mixed":
        for item in (mixed_ranges or {}).get("question_ranges") or []:
            qno = int(item["question_no"])
            mixed_labels = labels_for_shared_start_range(item, answer_packets)
            q_core_labels = [label for label in mixed_labels if label in question_blocks]
            q_labels = expand_labels_with_adjacent_context(q_core_labels, question_packets)
            q_surface_labels = question_surface_labels_from_full_range(labels_by_page_position(q_core_labels, question_blocks), question_blocks)
            assets = rule_visual_assets(q_core_labels, question_blocks)
            question_groups.append(
                {
                    "question_no": qno,
                    "block_labels": q_labels,
                    "core_block_labels": q_core_labels,
                    "question_surface_labels": q_surface_labels,
                    "visual_assets": assets,
                    "source": "v11_mixed_range_detector",
                }
            )
            rows.append(
                {
                    "question_no": qno,
                    "question_status": "found" if q_labels else "missing",
                    "answer_status": "unknown",
                    "question_labels": q_labels,
                    "question_core_labels": q_core_labels,
                    "question_surface_labels": q_surface_labels,
                    "visual_assets": [],
                    "answer_items": [],
                }
            )

    else:
        for item in (question_ranges or {}).get("question_ranges") or []:
            qno = int(item["question_no"])
            q_core_labels = labels_for_range(item, question_packets)
            q_labels = expand_labels_with_adjacent_context(q_core_labels, question_packets)
            q_surface_labels = question_surface_labels_from_full_range(labels_by_page_position(q_core_labels, question_blocks), question_blocks)
            assets = rule_visual_assets(q_core_labels, question_blocks)
            question_groups.append(
                {
                    "question_no": qno,
                    "block_labels": q_labels,
                    "core_block_labels": q_core_labels,
                    "question_surface_labels": q_surface_labels,
                    "visual_assets": assets,
                    "source": "v11_pure_paper_range_detector",
                }
            )
            rows.append(
                {
                    "question_no": qno,
                    "question_status": "found" if q_labels else "missing",
                    "answer_status": "missing",
                    "question_labels": q_labels,
                    "question_core_labels": q_core_labels,
                    "question_surface_labels": q_surface_labels,
                    "visual_assets": [],
                    "answer_items": [],
                }
            )

    qnos = {int(row["question_no"]) for row in rows}
    answer_qnos = qnos if pipeline_mode == "mixed" else {int(row["question_no"]) for row in rows if row.get("answer_items")}
    visual_summary = {"asset_count": 0, "assigned_count": 0, "noise_count": 0, "uncertain_count": 0, "risk_count": 0}
    alignment = {
        "qa_alignment": rows,
        "extra_answer_numbers": sorted(set()),
        "missing_answer_numbers": [] if pipeline_mode == "mixed" else sorted(qnos - answer_qnos),
        "visual_asset_assignment_summary": visual_summary,
        "asset_match_risk_count": int(visual_summary.get("risk_count") or 0),
    }
    write_json(out_run_dir / "question_groups.json", question_groups)
    write_json(out_run_dir / "qa_alignment.json", alignment)
    return alignment


def detect_pipeline_mode(source_run_dir: Path, forced: str) -> str:
    if forced != "auto":
        return forced
    if (source_run_dir / "answer" / "ocr_blocks.json").exists():
        return "paper_plus_answer_file"
    paper_ocr = read_json(source_run_dir / "paper" / "ocr_blocks.json", {})
    plan = classify_pages(paper_ocr)
    if plan.get("mode") == "interleaved_solution":
        return "mixed"
    return "pure_paper"


def run_one(
    run_id: str,
    source_runs_root: Path,
    output_root: Path,
    model: str,
    timeout: int,
    force: bool,
    mode: str,
    worker_models: list[str] | None = None,
    structured_output: str = "json_object",
    enable_thinking: bool = False,
    hide_block_text: bool = False,
) -> dict[str, Any]:
    source_runs_root = source_runs_root.resolve()
    output_root = output_root.resolve()
    source_run_dir = source_runs_root / run_id
    paper_dir = source_run_dir / "paper"
    answer_dir = source_run_dir / "answer"
    pipeline_mode = detect_pipeline_mode(source_run_dir, mode)
    out_run_dir = output_root / f"{run_id}_raw_units"

    paper_ocr = read_json(paper_dir / "ocr_blocks.json", {})
    paper_blocks = layout_items_for_doc(paper_ocr, paper_dir)
    if not paper_blocks:
        raise FileNotFoundError(f"missing paper OCR blocks for {run_id}")
    write_json(out_run_dir / "question_layout_items.json", paper_blocks)
    paper_namespace = "M" if pipeline_mode == "mixed" else "Q"
    question_packets = build_packets(
        run_id, paper_dir, out_run_dir, paper_blocks, paper_namespace, "question_packets", include_text=not hide_block_text
    )

    answer_blocks: list[dict[str, Any]] = []
    answer_packets: list[dict[str, Any]] = []
    question_ranges: dict[str, Any] | None = None
    answer_ranges: dict[str, Any] | None = None
    mixed_ranges: dict[str, Any] | None = None
    active_models = normalize_worker_models(model, worker_models)
    range_models: dict[str, str] = {}

    def range_model(index: int) -> str:
        return active_models[index % len(active_models)]

    if pipeline_mode == "paper_plus_answer_file":
        answer_ocr = read_json(answer_dir / "ocr_blocks.json", {})
        answer_blocks = layout_items_for_doc(answer_ocr, answer_dir)
        write_json(out_run_dir / "answer_layout_items.json", answer_blocks)
        answer_packets = build_packets(
            run_id, answer_dir, out_run_dir, answer_blocks, "A", "answer_packets", include_text=not hide_block_text
        )
        tasks = {
            "pure_paper": {
                "packets": question_packets,
                "stream": "paper",
                "mode": "pure_paper",
                "model": range_model(0),
                "out_path": out_run_dir / "step2_pure_paper_ranges.json",
            },
            "pure_answer": {
                "packets": answer_packets,
                "stream": "answer",
                "mode": "pure_answer",
                "model": range_model(1),
                "out_path": out_run_dir / "step2_pure_answer_ranges.json",
            },
        }
        range_models = {name: str(task["model"]) for name, task in tasks.items()}
        with ThreadPoolExecutor(max_workers=min(len(tasks), len(active_models))) as pool:
            future_map = {
                pool.submit(
                    call_range_detector,
                    task["packets"],
                    run_id=run_id,
                    stream=str(task["stream"]),
                    mode=str(task["mode"]),
                    model=str(task["model"]),
                    timeout=timeout,
                    out_path=Path(task["out_path"]),
                    force=force,
                    structured_output=structured_output,
                    enable_thinking=enable_thinking,
                ): name
                for name, task in tasks.items()
            }
            for future in as_completed(future_map):
                name = future_map[future]
                if name == "pure_paper":
                    question_ranges = future.result()
                elif name == "pure_answer":
                    answer_ranges = future.result()
    elif pipeline_mode == "mixed":
        write_json(out_run_dir / "answer_layout_items.json", paper_blocks)
        answer_packets = build_packets(
            run_id, paper_dir, out_run_dir, paper_blocks, "M", "answer_packets", include_text=not hide_block_text
        )
        range_models["mixed"] = range_model(0)
        mixed_ranges = call_range_detector(
            answer_packets,
            run_id=run_id,
            stream="paper",
            mode="mixed",
            model=range_models["mixed"],
            timeout=timeout,
            out_path=out_run_dir / "step2_mixed_ranges.json",
            force=force,
            structured_output=structured_output,
            enable_thinking=enable_thinking,
        )
    else:
        range_models["pure_paper"] = range_model(0)
        question_ranges = call_range_detector(
            question_packets,
            run_id=run_id,
            stream="paper",
            mode="pure_paper",
            model=range_models["pure_paper"],
            timeout=timeout,
            out_path=out_run_dir / "step2_pure_paper_ranges.json",
            force=force,
            structured_output=structured_output,
            enable_thinking=enable_thinking,
        )
        write_json(out_run_dir / "answer_layout_items.json", [])

    alignment = build_alignment(
        run_id=run_id,
        pipeline_mode=pipeline_mode,
        question_ranges=question_ranges,
        answer_ranges=answer_ranges,
        mixed_ranges=mixed_ranges,
        question_packets=question_packets,
        answer_packets=answer_packets,
        out_run_dir=out_run_dir,
    )
    range_summaries = {
        "pure_paper": question_ranges,
        "pure_answer": answer_ranges,
        "mixed": mixed_ranges,
    }
    visual_summary = (alignment.get("visual_asset_assignment_summary") or {}) if isinstance(alignment, dict) else {}
    summary = {
        "run_id": run_id,
        "mode": pipeline_mode,
        "model": multi_model_name(active_models),
        "worker_models": active_models,
        "range_models": range_models,
        "question_count": len(alignment.get("qa_alignment") or []),
        "answered_question_count": len(alignment.get("qa_alignment") or []) if pipeline_mode == "mixed" else sum(1 for row in alignment.get("qa_alignment") or [] if row.get("answer_items")),
        "missing_answer_numbers": [] if pipeline_mode == "mixed" else alignment.get("missing_answer_numbers") or [],
        "answer_tracking": "embedded_in_mixed_range" if pipeline_mode == "mixed" else "separate_answer_items",
        "range_missing_question_numbers": {
            key: value.get("missing_question_numbers_within_detected_span")
            for key, value in range_summaries.items()
            if value
        },
        "range_elapsed_seconds": {
            key: value.get("llm_elapsed_seconds")
            for key, value in range_summaries.items()
            if value
        },
        "step2_timeout_seconds": timeout,
    }
    write_json(out_run_dir / "pipeline_summary.json", summary)
    return summary




def main() -> None:
    parser = argparse.ArgumentParser(description="Step2 layout range detection for v11 question-bank pipeline.")
    parser.add_argument("--source-runs-root", type=Path, default=WORKTREE_ROOT / "source_runs")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--worker-model", action="append", default=[])
    parser.add_argument("--llm-provider", choices=["siliconflow", "bailian", "bailian_batch", "env"], default=DEFAULT_LLM_PROVIDER)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--tpm-limit", type=int, default=None, help="Local LLM tokens-per-minute limit. 0 disables.")
    parser.add_argument("--token-estimator-model", default=None, help="Tokenizer/processor model. Defaults to --model.")
    parser.add_argument("--image-token-mode", choices=["auto", "processor", "formula"], default="auto")
    parser.add_argument("--tpm-output-reserve", type=int, default=DEFAULT_OUTPUT_RESERVE_TOKENS)
    parser.add_argument("--token-budget-log", type=Path, default=None)
    parser.add_argument("--token-budget-verbose", action="store_true")
    parser.add_argument("--mode", choices=["auto", "pure_paper", "paper_plus_answer_file", "mixed"], default="auto")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--structured-output", choices=["json_object", "tool_calling"], default="tool_calling")
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--hide-block-text", action="store_true", help="Do not include OCR text or text-derived hints in Step2 LLM compact input.")
    args = parser.parse_args()

    configure_llm_provider_env(os.environ, args.llm_provider)
    configure_token_budget_env(
        os.environ,
        tpm_limit=args.tpm_limit,
        estimator_model=args.token_estimator_model or args.model,
        image_token_mode=args.image_token_mode,
        output_reserve_tokens=args.tpm_output_reserve,
        verbose=args.token_budget_verbose,
        log_path=args.token_budget_log,
    )
    summaries = [
        run_one(
            run_id=run_id,
            source_runs_root=args.source_runs_root,
            output_root=args.output_root,
            model=args.model,
            timeout=args.timeout,
            force=args.force,
            mode=args.mode,
            worker_models=args.worker_model or [args.model],
            structured_output=args.structured_output,
            enable_thinking=args.enable_thinking,
            hide_block_text=args.hide_block_text,
        )
        for run_id in args.run_id
    ]
    write_json(args.output_root / "batch_summary.json", {"runs": summaries, "model": args.model})
    print(json.dumps({"runs": summaries, "output_root": str(args.output_root)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
