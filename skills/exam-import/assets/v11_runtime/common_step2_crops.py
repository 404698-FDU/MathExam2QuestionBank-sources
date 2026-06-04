from __future__ import annotations

import re
import shutil
import math
from pathlib import Path
from typing import Any

from common_io import read_json


SOLUTION_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"【(?:思路分析|分析|解析|解答|答案|归纳总结|评析|点评)】"
    r"|(?:思路分析|分析|解析|解答|答案|归纳总结|评析|点评)\s*[:：]"
    r"|(?:解|证明|证|答)\s*[:：]"
    r")"
)


def base_line_block_id(block_id: Any) -> tuple[str, int] | None:
    match = re.fullmatch(r"(.+)_l(\d+)", str(block_id or ""))
    if not match:
        return None
    return match.group(1), int(match.group(2))


def display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if "\u4e00" <= char <= "\u9fff" else 1
    return width


def estimated_line_weight(text: str, bbox_width: float) -> int:
    capacity = max(30, int(bbox_width / 4))
    return max(1, (display_width(text) + capacity - 1) // capacity)


def effective_block_bboxes(blocks: list[dict[str, Any]]) -> dict[str, list[float]]:
    bboxes: dict[str, list[float]] = {}
    split_groups: dict[tuple[str, tuple[float, float, float, float]], list[dict[str, Any]]] = {}
    for block in blocks:
        label = str(block.get("label") or "")
        bbox = block.get("bbox")
        if not label or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        numeric_bbox = [float(value) for value in bbox]
        bboxes[label] = numeric_bbox
        line_info = base_line_block_id(block.get("block_id"))
        if not line_info:
            continue
        base_id, line_no = line_info
        grouped_block = dict(block)
        grouped_block["_line_no"] = line_no
        split_groups.setdefault((base_id, tuple(numeric_bbox)), []).append(grouped_block)

    for (_, bbox_tuple), group in split_groups.items():
        if len(group) <= 1:
            continue
        left, top, right, bottom = bbox_tuple
        if bottom - top <= 32:
            continue
        group.sort(key=lambda item: int(item["_line_no"]))
        weights = [estimated_line_weight(str(item.get("text") or ""), right - left) for item in group]
        total_weight = sum(weights)
        if total_weight <= 0:
            continue
        cursor = top
        for item, weight in zip(group, weights):
            height = (bottom - top) * weight / total_weight
            label = str(item.get("label") or "")
            if label:
                bboxes[label] = [left, cursor, right, min(bottom, cursor + height)]
            cursor += height
    return bboxes


def load_step2_label_index(block_root: Path, split_shared_bboxes: bool = True) -> dict[str, dict[str, Any]]:
    label_index: dict[str, dict[str, Any]] = {}
    for packet_dir_name, stream in (("question_packets", "question"), ("answer_packets", "answer")):
        packet_root = block_root / packet_dir_name
        if not packet_root.exists():
            continue
        for packet_path in sorted(packet_root.glob("page_*.json")):
            packet = read_json(packet_path)
            page = int(packet.get("page") or 0)
            annotated_image = str(packet.get("annotated_page_image") or "")
            page_image = str(packet.get("page_image") or infer_source_page_image(block_root, stream, page, annotated_image))
            blocks = packet.get("blocks", [])
            effective_bboxes = effective_block_bboxes(blocks) if split_shared_bboxes else {}
            for block in blocks:
                label = str(block.get("label") or "")
                bbox = effective_bboxes.get(label) or block.get("bbox")
                if label and bbox:
                    label_index[label] = {
                        "stream": stream,
                        "page": page,
                        "bbox": bbox,
                        "annotated_image": annotated_image,
                        "page_image": page_image,
                        "is_asset": is_asset_block(block),
                    }
    return label_index


def infer_source_page_image(block_root: Path, stream: str, page: int, annotated_image: str = "") -> Path:
    run_id = str(block_root.name).removesuffix("_raw_units")
    annotated_path = Path(annotated_image) if annotated_image else None
    if annotated_path is not None and annotated_path.is_absolute():
        try:
            runs_root = annotated_path.parent.parent.parent.parent
        except IndexError:
            runs_root = block_root.parent.parent
    else:
        runs_root = block_root.parent.parent
    side = "answer" if stream == "answer" else "paper"
    return runs_root / "source_runs" / run_id / side / "pages" / f"page_{page:03d}.png"


def is_asset_block(block: dict[str, Any]) -> bool:
    label = str(block.get("label") or block.get("local_label") or "")
    local_label = label.rsplit("-", 1)[-1]
    block_type = str(block.get("type") or "").lower()
    return (
        local_label.startswith("P")
        or bool(block.get("figure_id"))
        or any(marker in block_type for marker in ("image", "figure", "table"))
    )


def draw_asset_box(draw: Any, bbox: tuple[float, float, float, float], label: str) -> None:
    x1, y1, x2, y2 = [int(round(value)) for value in bbox]
    color = (214, 82, 51, 255)
    draw.rectangle((x1, y1, x2, y2), outline=color, width=3)
    text_bbox = draw.textbbox((x1, y1), label)
    pad = 2
    label_h = text_bbox[3] - text_bbox[1]
    label_w = text_bbox[2] - text_bbox[0]
    label_left = x1 + 2
    label_top = y1 + 2
    label_right = min(x2, label_left + label_w + 2 * pad)
    label_bottom = min(y2, label_top + label_h + 2 * pad)
    if label_right > label_left and label_bottom > label_top:
        draw.rectangle((label_left, label_top, label_right, label_bottom), fill=(214, 82, 51, 180))
        draw.text((label_left + pad, label_top + pad), label, fill=(255, 255, 255, 255))


def union_bbox(bboxes: list[list[float]], margin: int, width: int, height: int) -> tuple[int, int, int, int] | None:
    if not bboxes:
        return None
    left = max(0, math.floor(min(box[0] for box in bboxes)) - margin)
    top = max(0, math.floor(min(box[1] for box in bboxes)) - margin)
    right = min(width, math.ceil(max(box[2] for box in bboxes)) + margin)
    bottom = min(height, math.ceil(max(box[3] for box in bboxes)) + margin)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def scale_bbox_to_image(
    bbox: list[float],
    width: int,
    height: int,
    coord_width: float = 1000.0,
    coord_height: float = 1000.0,
) -> list[float]:
    left, top, right, bottom = [float(value) for value in bbox]
    return [
        left / coord_width * width,
        top / coord_height * height,
        right / coord_width * width,
        bottom / coord_height * height,
    ]


def unique_labels(value: Any) -> list[str]:
    labels: list[str] = []
    if not isinstance(value, list):
        return labels
    for item in value:
        label = str(item or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def block_by_label_from_packets(block_root: Path, packet_dir_name: str = "question_packets") -> dict[str, dict[str, Any]]:
    blocks: dict[str, dict[str, Any]] = {}
    packet_root = block_root / packet_dir_name
    if not packet_root.exists():
        return blocks
    for packet_path in sorted(packet_root.glob("page_*.json")):
        packet = read_json(packet_path)
        page = int(packet.get("page") or 0)
        for order, block in enumerate(packet.get("blocks") or []):
            label = str(block.get("label") or "")
            if not label:
                continue
            row = dict(block)
            row.setdefault("page", page)
            row["_packet_page"] = page
            row["_packet_order"] = order
            blocks[label] = row
    return blocks


def is_solution_marker_text(text: str) -> bool:
    return bool(SOLUTION_MARKER_RE.match(text.strip()))


def question_surface_labels_from_full_range(
    labels: list[str],
    blocks_by_label: dict[str, dict[str, Any]],
) -> list[str]:
    surface: list[str] = []
    for label in unique_labels(labels):
        block = blocks_by_label.get(label) or {}
        text = str(block.get("text") or "")
        if is_solution_marker_text(text):
            if not surface:
                continue
            break
        surface.append(label)
    return surface


def question_group_range_labels(group: dict[str, Any]) -> list[str]:
    labels = list(group.get("block_labels") or [])
    for asset in group.get("visual_assets") or []:
        if isinstance(asset, dict) and asset.get("label"):
            labels.append(str(asset["label"]))
    return unique_labels(labels)


def load_step2_question_range_labels(block_root: Path) -> dict[int, list[str]]:
    groups = read_json(block_root / "question_groups.json", None)
    if not isinstance(groups, list):
        return {}
    labels_by_qno: dict[int, list[str]] = {}
    for group in groups:
        if not isinstance(group, dict) or group.get("question_no") is None:
            continue
        labels_by_qno[int(group["question_no"])] = question_group_range_labels(group)
    return labels_by_qno


def load_step2_question_surface_labels(block_root: Path) -> dict[int, list[str]]:
    groups = read_json(block_root / "question_groups.json", None)
    if not isinstance(groups, list):
        return {}
    blocks_by_label = block_by_label_from_packets(block_root, "question_packets")
    labels_by_qno: dict[int, list[str]] = {}
    for group in groups:
        if not isinstance(group, dict) or group.get("question_no") is None:
            continue
        labels = list(group.get("question_surface_labels") or [])
        if not labels:
            labels = question_group_range_labels(group)
            labels = question_surface_labels_from_full_range(labels, blocks_by_label)
        labels_by_qno[int(group["question_no"])] = unique_labels(labels)
    return labels_by_qno


def crop_labels_to_images(
    run_id: str,
    qno: int,
    labels: list[str],
    run_block_dir: Path,
    crop_dir: Path,
    filename_role: str,
    clear_existing: bool = False,
    margin: int = 8,
    split_shared_bboxes: bool = True,
    asset_boxes_only: bool = False,
) -> list[Path]:
    if not labels or not run_block_dir.exists():
        return []
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to render Step2 crop images") from exc

    if clear_existing and crop_dir.exists():
        shutil.rmtree(crop_dir)
    crop_dir.mkdir(parents=True, exist_ok=True)

    label_index = load_step2_label_index(run_block_dir, split_shared_bboxes=split_shared_bboxes)
    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for label in unique_labels(labels):
        item = label_index.get(label)
        if not item:
            continue
        image_path = str(item.get("page_image") if asset_boxes_only else item.get("annotated_image") or "")
        if not image_path:
            continue
        group_key = (str(item.get("stream") or ""), int(item.get("page") or 0), image_path)
        grouped.setdefault(group_key, []).append({"label": label, "bbox": item["bbox"], "is_asset": bool(item.get("is_asset"))})

    paths: list[Path] = []
    image_cache: dict[Path, Any] = {}
    try:
        for crop_index, ((stream, page, image_path), items) in enumerate(sorted(grouped.items()), start=1):
            source_image = Path(image_path)
            if not source_image.exists():
                continue
            image = image_cache.get(source_image)
            if image is None:
                image = Image.open(source_image)
                image_cache[source_image] = image
            scaled_items = [
                {
                    "label": str(item.get("label") or ""),
                    "bbox": scale_bbox_to_image(item["bbox"], image.width, image.height),
                    "is_asset": bool(item.get("is_asset")),
                }
                for item in items
            ]
            scaled_bboxes = [item["bbox"] for item in scaled_items]
            bbox = union_bbox(scaled_bboxes, margin=margin, width=image.width, height=image.height)
            if not bbox:
                continue
            filename = f"{run_id}_q{qno:02d}_{filename_role}_{stream}_p{page:03d}_{crop_index}.png"
            dest = crop_dir / filename
            cropped = image.crop(bbox).convert("RGB")
            if asset_boxes_only:
                from PIL import ImageDraw

                draw = ImageDraw.Draw(cropped, "RGBA")
                left, top, _right, _bottom = bbox
                for item in scaled_items:
                    if not item.get("is_asset"):
                        continue
                    x1, y1, x2, y2 = item["bbox"]
                    draw_asset_box(draw, (x1 - left, y1 - top, x2 - left, y2 - top), str(item.get("label") or "asset"))
            cropped.save(dest)
            paths.append(dest)
    finally:
        for image in image_cache.values():
            image.close()
    return paths
