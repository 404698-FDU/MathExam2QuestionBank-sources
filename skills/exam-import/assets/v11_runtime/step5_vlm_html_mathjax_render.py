#!/usr/bin/env python3
"""Render standardized question-bank JSON into browser HTML with MathJax.

This is a review renderer, not a PDF/typesetting backend. It keeps TeX math as
text for MathJax to process in the browser, so alignment characters inside
environments such as aligned/cases are not rewritten by the LaTeX PDF renderer.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKTREE_ROOT = Path(__file__).resolve().parent
DEFAULT_QB_ROOT = WORKTREE_ROOT / "runs_question_bank"
DEFAULT_OUTPUT_ROOT = WORKTREE_ROOT / "rendered_question_bank_mathjax"
DEFAULT_ASSET_ROOT = DEFAULT_OUTPUT_ROOT
DEFAULT_BLOCK_ROOT = WORKTREE_ROOT / "runs_step2_exam_blocks"
DEFAULT_SOURCE_RUNS = WORKTREE_ROOT / "source_runs"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common_io import read_json, write_text  # noqa: E402
from common_math_text import split_math_segments  # noqa: E402
from common_step2_crops import load_step2_question_range_labels, load_step2_question_surface_labels, scale_bbox_to_image  # noqa: E402
import shutil

def copy_asset(source: Path, dest: Path) -> bool:
    if not source.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


def copy_common_template(out_dir: Path) -> None:
    text = COMMON_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace(
        r"\documentclass[10pt,UTF8]{ctexart}",
        r"\documentclass[10pt,UTF8,fontset=fandol]{ctexart}",
        1,
    )
    write_text(out_dir / COMMON_TEMPLATE.name, text)


def iter_packet_blocks(packet_root: Path) -> dict[str, dict[str, Any]]:
    blocks_by_label: dict[str, dict[str, Any]] = {}
    if not packet_root.exists():
        return blocks_by_label
    for packet_path in sorted(packet_root.glob("page_*.json")):
        packet = read_json(packet_path)
        for block in packet.get("blocks", []):
            label = block.get("label")
            if label:
                blocks_by_label[str(label)] = block
    return blocks_by_label


def load_step2_label_index(block_root: Path, split_shared_bboxes: bool = True) -> dict[str, dict[str, Any]]:
    label_index: dict[str, dict[str, Any]] = {}
    for packet_dir_name, stream in (("question_packets", "question"), ("answer_packets", "answer")):
        packet_root = block_root / packet_dir_name
        if not packet_root.exists():
            continue
        for packet_path in sorted(packet_root.glob("page_*.json")):
            packet = read_json(packet_path)
            page = int(packet.get("page") or 0)
            annotated_image = packet.get("annotated_page_image")
            blocks = packet.get("blocks", [])
            effective_bboxes = effective_block_bboxes(blocks) if split_shared_bboxes else {}
            for block in blocks:
                label = block.get("label")
                bbox = effective_bboxes.get(str(label or "")) or block.get("bbox")
                if label and bbox:
                    label_index[str(label)] = {
                        "stream": stream,
                        "page": page,
                        "bbox": bbox,
                        "annotated_image": str(annotated_image or ""),
                    }
    return label_index


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
        key = (base_id, tuple(numeric_bbox))
        grouped_block = dict(block)
        grouped_block["_line_no"] = line_no
        split_groups.setdefault(key, []).append(grouped_block)

    for (_, bbox_tuple), group in split_groups.items():
        if len(group) <= 1:
            continue
        left, top, right, bottom = bbox_tuple
        if bottom - top <= 32:
            continue
        group.sort(key=lambda item: int(item["_line_no"]))
        weights = [
            estimated_line_weight(str(item.get("text") or ""), right - left)
            for item in group
        ]
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


def unique_labels(value: Any) -> list[str]:
    labels: list[str] = []
    if not isinstance(value, list):
        return labels
    for item in value:
        label = str(item or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


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


def build_step2_crop_map(
    run_id: str,
    records: list[dict[str, Any]],
    out_dir: Path,
    run_block_dir: Path,
) -> dict[int, dict[str, list[Path]]]:
    if not run_block_dir.exists():
        return {}
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to render Step2 crop images") from exc

    split_label_index = load_step2_label_index(run_block_dir, split_shared_bboxes=True)
    full_label_index = load_step2_label_index(run_block_dir, split_shared_bboxes=False)
    if not split_label_index and not full_label_index:
        return {}
    pipeline_summary = read_json(run_block_dir / "pipeline_summary.json", {})
    pipeline_mode = str(pipeline_summary.get("mode") or "")
    question_range_labels_by_qno = load_step2_question_range_labels(run_block_dir)
    question_surface_labels_by_qno = load_step2_question_surface_labels(run_block_dir)

    crop_dir = out_dir / "step2_crops"
    if crop_dir.exists():
        shutil.rmtree(crop_dir)
    crop_dir.mkdir(parents=True, exist_ok=True)

    crops: dict[int, dict[str, list[Path]]] = {}
    image_cache: dict[Path, Any] = {}

    for record in records:
        qno = int(record.get("question_no") or 0)
        if qno <= 0:
            continue
        crops[qno] = {"question": [], "answer": []}
        label_fields = (
            ("question", "step2_question_surface_labels"),
            ("answer", "source_answer_labels"),
        )
        for stream, field_name in label_fields:
            if stream == "question":
                labels = unique_labels(question_surface_labels_by_qno.get(qno) or [])
            else:
                labels = unique_labels(record.get(field_name))
                if not labels and pipeline_mode == "mixed":
                    surface = set(unique_labels(question_surface_labels_by_qno.get(qno) or []))
                    labels = [
                        label
                        for label in unique_labels(question_range_labels_by_qno.get(qno) or [])
                        if label not in surface
                    ]
            label_index = full_label_index if stream == "question" else split_label_index
            grouped: dict[tuple[str, int, str], list[list[float]]] = {}
            for label in labels:
                item = label_index.get(label)
                if not item:
                    continue
                annotated_image = item.get("annotated_image") or ""
                if not annotated_image:
                    continue
                group_key = (stream, int(item.get("page") or 0), annotated_image)
                grouped.setdefault(group_key, []).append(item["bbox"])
            for crop_index, ((_, page, image_path), bboxes) in enumerate(sorted(grouped.items()), start=1):
                source_image = Path(image_path)
                if not source_image.exists():
                    continue
                image = image_cache.get(source_image)
                if image is None:
                    image = Image.open(source_image)
                    image_cache[source_image] = image
                scaled_bboxes = [scale_bbox_to_image(bbox, image.width, image.height) for bbox in bboxes]
                bbox = union_bbox(scaled_bboxes, margin=0 if stream == "question" else 8, width=image.width, height=image.height)
                if not bbox:
                    continue
                cropped = image.crop(bbox)
                filename = f"{run_id}_q{qno:02d}_{stream}_p{page:03d}_{crop_index}.png"
                dest = crop_dir / filename
                cropped.save(dest)
                crops[qno][stream].append(dest)

    for image in image_cache.values():
        image.close()
    return crops


def load_layout_index(block_root: Path, filename: str) -> dict[str, dict[str, Any]]:
    layout_path = block_root / filename
    if not layout_path.exists():
        return {}
    items = read_json(layout_path)
    index: dict[str, dict[str, Any]] = {}
    for item in items:
        block_id = item.get("block_id")
        figure_id = item.get("figure_id")
        if block_id:
            index[str(block_id)] = item
        if figure_id:
            index[str(figure_id)] = item
    return index


def load_question_layout_index(block_root: Path) -> dict[str, dict[str, Any]]:
    return load_layout_index(block_root, "question_layout_items.json")


def find_source_path(layout_item: dict[str, Any], source_run: Path) -> Path | None:
    raw_path = layout_item.get("path")
    mineru = layout_item.get("mineru_item") or {}
    if not raw_path:
        raw_path = mineru.get("img_path")
    if not raw_path:
        return None
    raw = str(raw_path).replace("/", "\\")
    candidates = [
        source_run / "paper" / raw,
        source_run / "paper" / "mineru_extract" / raw,
        source_run / "paper" / "mineru_extract" / "images" / Path(raw).name,
        source_run / "answer" / raw,
        source_run / "answer" / "mineru_extract" / raw,
        source_run / "answer" / "mineru_extract" / "images" / Path(raw).name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def build_asset_map(run_id: str, out_dir: Path, block_root: Path, source_run: Path) -> dict[str, Path]:
    label_to_rel_path: dict[str, Path] = {}
    assets_dir = out_dir / "assets"

    packet_sources = [
        (block_root / "question_packets", load_layout_index(block_root, "question_layout_items.json")),
        (block_root / "answer_packets", load_layout_index(block_root, "answer_layout_items.json")),
    ]
    for packet_root, layout_index in packet_sources:
        for label, packet_block in iter_packet_blocks(packet_root).items():
            if label in label_to_rel_path:
                continue
            block_type = str(packet_block.get("type", "")).lower()
            if block_type not in {"image", "chart", "table", "interline_equation"}:
                continue
            layout_item = None
            for key in (packet_block.get("block_id"), packet_block.get("figure_id")):
                if key and str(key) in layout_index:
                    layout_item = layout_index[str(key)]
                    break
            if not layout_item:
                continue
            source = find_source_path(layout_item, source_run)
            if not source:
                continue
            suffix = source.suffix or ".png"
            filename = f"{run_id}_{label}{suffix}"
            dest = assets_dir / filename
            if copy_asset(source, dest):
                label_to_rel_path[label] = Path("assets") / filename
    return label_to_rel_path

RAW_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def as_blocks(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def rel(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_uri()


class RawHtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self._table_depth = 0
        self._current_row: list[dict[str, Any]] | None = None
        self._current_cell: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            return
        if self._table_depth <= 0:
            return
        if tag == "tr":
            self._finish_cell()
            self._finish_row()
            self._current_row = []
            return
        if tag in {"td", "th"} and self._current_row is not None:
            self._finish_cell()
            self._current_cell = {
                "tag": tag,
                "attrs": self._clean_cell_attrs(attrs),
                "parts": [],
            }
            return
        if tag == "br" and self._current_cell is not None:
            self._current_cell["parts"].append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br" and self._current_cell is not None:
            self._current_cell["parts"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"}:
            self._finish_cell()
            return
        if tag == "tr":
            self._finish_cell()
            self._finish_row()
            return
        if tag == "table":
            self._finish_cell()
            self._finish_row()
            self._table_depth = max(0, self._table_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell["parts"].append(data)

    @staticmethod
    def _clean_cell_attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for name, value in attrs:
            name = name.lower()
            if name not in {"colspan", "rowspan"} or value is None:
                continue
            if re.fullmatch(r"\d{1,2}", value.strip()):
                amount = max(1, min(20, int(value.strip())))
                if amount > 1:
                    cleaned[name] = str(amount)
        return cleaned

    def _finish_cell(self) -> None:
        if self._current_cell is None or self._current_row is None:
            self._current_cell = None
            return
        text = re.sub(r"\s+", " ", "".join(self._current_cell["parts"])).strip()
        self._current_row.append(
            {
                "tag": self._current_cell["tag"],
                "attrs": self._current_cell["attrs"],
                "text": text,
            }
        )
        self._current_cell = None

    def _finish_row(self) -> None:
        if self._current_row is not None and any(cell.get("text") for cell in self._current_row):
            self.rows.append(self._current_row)
        self._current_row = None


def raw_html_table_to_html(table_html: str) -> str | None:
    parser = RawHtmlTableParser()
    try:
        parser.feed(table_html)
        parser.close()
    except Exception:  # noqa: BLE001
        return None
    if not parser.rows:
        return None
    rows: list[str] = []
    for row in parser.rows:
        cells: list[str] = []
        for cell in row:
            tag = "th" if cell.get("tag") == "th" else "td"
            attrs = "".join(f' {name}="{esc(value)}"' for name, value in sorted((cell.get("attrs") or {}).items()))
            cells.append(f"<{tag}{attrs}>{render_inline_text(str(cell.get('text') or ''))}</{tag}>")
        if cells:
            rows.append("<tr>" + "".join(cells) + "</tr>")
    if not rows:
        return None
    return '<div class="table-wrap"><table class="html-table">' + "".join(rows) + "</table></div>"


def iter_table_matches(text: str) -> list[tuple[int, int, str]]:
    return [(match.start(), match.end(), match.group(0)) for match in RAW_HTML_TABLE_RE.finditer(text)]


def render_plain_text_with_blanks(text: str) -> str:
    text = esc(text)
    text = re.sub(r"[\(（]\s*_{2,}\s*[\)）]", '<span class="choice-blank"><span class="choice-blank-space"></span></span>', text)
    return re.sub(r"_{2,}", '<span class="blank"></span>', text)


def render_text_segment(text: str) -> str:
    text = text.replace(r"\blankline{}", "____")
    parts: list[str] = []
    last = 0
    for match in re.finditer(r"<(?:blank|choice_blank)>", text):
        before = text[last : match.start()]
        before = render_plain_text_with_blanks(before)
        parts.append(before)
        if match.group(0) == "<choice_blank>":
            parts.append('<span class="choice-blank"><span class="choice-blank-space"></span></span>')
        else:
            parts.append('<span class="blank"></span>')
        last = match.end()
    tail = text[last:]
    tail = render_plain_text_with_blanks(tail)
    parts.append(tail)
    return "".join(parts).replace("\n", "<br>")


def render_math_segment(text: str) -> str:
    blank_tex = r"\underline{\hspace{3em}}"
    choice_blank_tex = r"(\hspace{1.8em})"
    text = text.replace(r"\blankline{}", blank_tex)
    text = text.replace("<choice_blank>", choice_blank_tex).replace("<blank>", blank_tex)
    text = re.sub(r"_{2,}", lambda _match: blank_tex, text)
    return esc(text)


def render_inline_text(text: str) -> str:
    return "".join(
        render_math_segment(segment) if is_math else render_text_segment(segment)
        for is_math, segment in split_math_segments(text)
    )



def render_block(text: str) -> str:
    parts: list[str] = []
    last = 0
    for start, end, value in iter_table_matches(text):
        before = text[last:start].strip()
        if before:
            parts.append(f'<p class="text-block">{render_inline_text(before)}</p>')
        table = raw_html_table_to_html(value)
        if table:
            parts.append(table)
        last = end
    tail = text[last:].strip()
    if tail:
        parts.append(f'<p class="text-block">{render_inline_text(tail)}</p>')
    if parts:
        return "".join(parts)
    return raw_html_table_to_html(text) or ""


def asset_path(run_id: str, label: str, asset_root: Path) -> Path | None:
    assets_dir = asset_root / run_id / "assets"
    if not assets_dir.exists():
        return None
    matches = sorted(assets_dir.glob(f"{run_id}_{label}.*"))
    return matches[0] if matches else None


def render_asset(run_id: str, label: str, asset_root: Path, out_path: Path, inline: bool = False) -> str:
    path = asset_path(run_id, label, asset_root)
    if not path:
        return f'<span class="missing-asset">{esc(label)}</span>'
    css_class = "option-asset" if inline else "figure"
    return f'<img class="{css_class}" src="{esc(rel(path, out_path.parent))}" alt="{esc(label)}">'


def render_blocks(blocks: Any, run_id: str, record: dict[str, Any], asset_root: Path, out_path: Path, option_key: str | None = None) -> str:
    html_blocks: list[str] = []
    replacements: dict[str, str] = {}
    for asset in record.get("visual_assets") or []:
        if not isinstance(asset, dict) or not asset.get("label"):
            continue
        label = str(asset["label"])
        replacements[label] = render_asset(run_id, label, asset_root, out_path, inline=bool(option_key))
    if option_key:
        option_assets = record.get("option_asset_placeholders") or {}
        asset = option_assets.get(option_key) if isinstance(option_assets, dict) else None
        if isinstance(asset, dict) and asset.get("label"):
            label = str(asset["label"])
            rendered_asset = render_asset(run_id, label, asset_root, out_path, inline=True)
            replacements[label] = rendered_asset
            if asset.get("placeholder"):
                replacements[str(asset["placeholder"])] = rendered_asset
    for block in as_blocks(blocks):
        rendered = render_block(block)
        for placeholder, replacement in replacements.items():
            escaped_placeholder = esc(placeholder)
            if escaped_placeholder in rendered:
                rendered = rendered.replace(escaped_placeholder, replacement)
            elif placeholder in rendered:
                rendered = rendered.replace(placeholder, replacement)
        if rendered:
            html_blocks.append(rendered)
    return "".join(html_blocks) if html_blocks else '<p class="empty">(empty)</p>'


def render_options(run_id: str, record: dict[str, Any], asset_root: Path, out_path: Path) -> str:
    options = record.get("options_latex") if isinstance(record.get("options_latex"), dict) else {}
    option_assets = record.get("option_asset_placeholders") if isinstance(record.get("option_asset_placeholders"), dict) else {}
    option_keys = [
        key
        for key in ("A", "B", "C", "D")
        if as_blocks(options.get(key)) or key in option_assets
    ]
    if not option_keys:
        return ""
    rows = []
    for key in option_keys:
        value = options.get(key)
        asset = option_assets.get(key)
        placeholder = asset.get("placeholder") if isinstance(asset, dict) else None
        asset_label = str(asset.get("label") or "") if isinstance(asset, dict) else ""
        if placeholder:
            blocks = as_blocks(value)
            only_caption = not blocks or all(
                re.fullmatch(rf"{re.escape(key)}\s*[.．、:]?", block.strip())
                or re.fullmatch(r"<(?:image|chart|figure)\d+>", block.strip(), re.IGNORECASE)
                for block in blocks
            )
            if only_caption:
                value = [str(placeholder)]
            elif str(placeholder) not in blocks:
                value = blocks + [str(placeholder)]
        elif value is None:
            value = [] if asset_label else None
        if value is None:
            continue
        blocks = as_blocks(value)
        if not blocks and not asset_label:
            continue
        body = "" if not blocks else render_blocks(blocks, run_id, record, asset_root, out_path, option_key=key)
        asset_present = (placeholder and str(placeholder) in blocks) or (asset_label and asset_label in blocks)
        if asset_label and not asset_present:
            body += render_asset(run_id, asset_label, asset_root, out_path, inline=True)
        rows.append(
            '<div class="option">'
            f'<div class="option-key">{esc(key)}.</div>'
            f'<div class="option-body">{body}</div>'
            "</div>"
        )
    return '<div class="options">' + "".join(rows) + "</div>" if rows else ""


def visual_asset_labels(record: dict[str, Any], roles: set[str]) -> list[str]:
    labels: list[str] = []
    for asset in record.get("visual_assets") or []:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("role") or "") in roles and asset.get("label"):
            label = str(asset.get("label"))
            if label not in labels:
                labels.append(label)
    return labels


def render_figures(
    run_id: str,
    record: dict[str, Any],
    asset_root: Path,
    out_path: Path,
    roles: set[str] | None = None,
) -> str:
    roles = roles or {"stem_figure"}
    figures = visual_asset_labels(record, roles)
    if not figures:
        return ""
    return '<div class="figures">' + "".join(render_asset(run_id, label, asset_root, out_path) for label in figures) + "</div>"


def render_asset_section(
    title: str,
    roles: set[str],
    run_id: str,
    record: dict[str, Any],
    asset_root: Path,
    out_path: Path,
    class_name: str,
) -> str:
    body = render_figures(run_id, record, asset_root, out_path, roles=roles)
    if not body:
        return ""
    return f'<div class="qa-section {class_name}"><h3>{esc(title)}</h3><div class="qa-body">{body}</div></div>'


def render_content_section(
    title: str,
    blocks: Any,
    run_id: str,
    record: dict[str, Any],
    asset_root: Path,
    out_path: Path,
    class_name: str,
    collapsible: bool = False,
) -> str:
    if not as_blocks(blocks):
        return ""
    body = render_blocks(blocks, run_id, record, asset_root, out_path)
    if collapsible:
        return f'<details class="qa-section {class_name}"><summary>{esc(title)}</summary><div class="qa-body">{body}</div></details>'
    return f'<div class="qa-section {class_name}"><h3>{esc(title)}</h3><div class="qa-body">{body}</div></div>'


def issue_summary(record: dict[str, Any]) -> str:
    issues = record.get("issues") or []
    if not issues:
        return ""
    items = []
    for issue in issues:
        if isinstance(issue, dict):
            severity = issue.get("severity") or "warning"
            issue_type = issue.get("type") or "issue"
            if issue_type == "left_right_present":
                continue
            message = issue.get("message") or ""
            items.append(f"<li><b>{esc(severity)}</b> {esc(issue_type)}: {esc(message)}</li>")
        else:
            items.append(f"<li>{esc(issue)}</li>")
    if not items:
        return ""
    return "<details class=\"issues\"><summary>Issues</summary><ul>" + "".join(items) + "</ul></details>"


def render_step2_crop_section(qno: int, step2_crops: dict[int, dict[str, list[Path]]], out_path: Path) -> str:
    crops = step2_crops.get(qno) or {}
    question_crops = crops.get("question") or []
    answer_crops = crops.get("answer") or []
    if not question_crops and not answer_crops:
        return ""

    def render_group(title: str, paths: list[Path]) -> str:
        if not paths:
            return ""
        images = "".join(
            '<figure class="step2-crop-card">'
            f'<img src="{esc(rel(path, out_path.parent))}" alt="{esc(title)} Q{qno}">'
            "</figure>"
            for path in paths
        )
        return f'<div class="step2-crop-group"><h3>{esc(title)}</h3><div class="step2-crop-grid">{images}</div></div>'

    return (
        '<details class="step2-crops">'
        "<summary>Step2 裁剪</summary>"
        f"{render_group('题面', question_crops)}"
        f"{render_group('答案/解析', answer_crops)}"
        "</details>"
    )


def render_question(
    run_id: str,
    record: dict[str, Any],
    asset_root: Path,
    out_path: Path,
    step2_crops: dict[int, dict[str, list[Path]]],
) -> str:
    qno = int(record.get("question_no") or 0)
    qtype = str(record.get("question_type") or "unknown")
    return (
        f'<section class="question" id="q{qno:02d}">'
        f'<h2>Q{qno} <span>{esc(qtype)}</span></h2>'
        f"{render_step2_crop_section(qno, step2_crops, out_path)}"
        f'<div class="stem">{render_blocks(record.get("stem_latex"), run_id, record, asset_root, out_path)}</div>'
        f"{render_options(run_id, record, asset_root, out_path)}"
        f"{render_figures(run_id, record, asset_root, out_path, roles={'stem_figure'})}"
        f"{render_content_section('答案', record.get('answer_latex'), run_id, record, asset_root, out_path, 'answer-section')}"
        f"{render_asset_section('答案配图', {'answer_figure'}, run_id, record, asset_root, out_path, 'answer-figures')}"
        f"{render_content_section('解析', record.get('analysis_latex'), run_id, record, asset_root, out_path, 'analysis-section', collapsible=True)}"
        f"{render_asset_section('解析配图', {'analysis_figure'}, run_id, record, asset_root, out_path, 'analysis-figures')}"
        f"{render_content_section('评分标准', record.get('rubric_latex'), run_id, record, asset_root, out_path, 'rubric-section', collapsible=True)}"
        f"{issue_summary(record)}"
        "</section>"
    )


def read_records(qb_path: Path) -> list[dict[str, Any]]:
    payload = read_json(qb_path)
    records = payload.get("questions", []) if isinstance(payload, dict) else payload
    return sorted(records, key=lambda item: int(item.get("question_no") or 0))


def sync_assets(run_id: str, output_root: Path, block_root: Path, source_runs: Path) -> bool:
    run_block_dir = block_root / f"{run_id}_raw_units"
    source_run = source_runs / run_id
    if not run_block_dir.exists() or not source_run.exists():
        return False
    build_asset_map(run_id, output_root / run_id, run_block_dir, source_run)
    return True


def render_run(
    run_id: str,
    qb_root: Path,
    asset_root: Path,
    output_root: Path,
    block_root: Path,
    source_runs: Path,
    asset_sync: bool,
) -> Path:
    records = read_records(qb_root / run_id / "question_bank.json")
    out_path = output_root / run_id / "index.html"
    if asset_sync and sync_assets(run_id, output_root, block_root, source_runs):
        asset_root = output_root
    run_block_dir = block_root / f"{run_id}_raw_units"
    step2_crops = build_step2_crop_map(run_id, records, output_root / run_id, run_block_dir)
    links = "".join(f'<a href="#q{int(record.get("question_no") or 0):02d}">Q{int(record.get("question_no") or 0)}</a>' for record in records)
    questions = "".join(render_question(run_id, record, asset_root, out_path, step2_crops) for record in records)
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{esc(run_id)} MathJax Review</title>
<script>
window.MathJax = {{
  tex: {{
    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
    processEscapes: true,
    processEnvironments: true
  }},
  options: {{
    skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  }}
}};
</script>
<script defer src="https://cdn.jsdelivr.net/npm/mathjax@4/tex-chtml.js"></script>
<style>
body {{ margin: 0; background: #f5f6f8; color: #17202a; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; }}
header {{ position: sticky; top: 0; z-index: 5; background: #fff; border-bottom: 1px solid #d9dee7; padding: 14px 22px; }}
h1 {{ margin: 0 0 10px; font-size: 21px; }}
nav {{ display: flex; flex-wrap: wrap; gap: 6px; }}
nav a {{ padding: 4px 8px; border: 1px solid #cdd6e1; border-radius: 4px; color: #165e96; text-decoration: none; background: #f8fbff; font-size: 13px; }}
main {{ max-width: 980px; margin: 0 auto; padding: 18px; }}
.question {{ background: #fff; border: 1px solid #d9dee7; border-radius: 6px; padding: 14px 16px; margin: 0 0 14px; }}
.question h2 {{ margin: 0 0 10px; font-size: 18px; }}
.question h2 span {{ margin-left: 8px; padding: 2px 6px; border: 1px solid #d4dce7; border-radius: 4px; color: #596575; font-size: 12px; font-weight: 500; }}
.text-block {{ margin: 0 0 8px; line-height: 1.72; }}
.blank {{ display: inline-block; min-width: 3.4em; height: 0.72em; border-bottom: 1px solid #17202a; vertical-align: -0.08em; }}
.choice-blank {{ display: inline-flex; align-items: baseline; gap: 0.1em; vertical-align: -0.08em; }}
.choice-blank::before {{ content: "("; }}
.choice-blank::after {{ content: ")"; }}
.choice-blank-space {{ display: inline-block; min-width: 1.8em; height: 0.72em; }}
.options {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 18px; margin: 8px 0 4px; }}
.option {{ display: grid; grid-template-columns: 30px minmax(0, 1fr); align-items: start; }}
.option-key {{ font-weight: 700; color: #165e96; padding-top: 2px; }}
.option-body .text-block {{ margin: 0; }}
.figures {{ margin: 10px 0 0; display: flex; flex-wrap: wrap; align-items: flex-start; gap: 10px; }}
.figure {{ display: block; width: auto; height: auto; max-width: min(420px, 90%); border: 1px solid #dce3ec; border-radius: 4px; background: #fff; padding: 4px; object-fit: contain; }}
.option-asset {{ display: inline-block; width: auto; height: auto; max-width: 100%; max-height: 140px; vertical-align: middle; object-fit: contain; }}
.step2-crops {{ margin: 8px 0 14px; border: 1px solid #d9e1eb; border-radius: 6px; background: #f8fafc; padding: 8px 10px; }}
.step2-crops summary {{ cursor: pointer; color: #165e96; font-weight: 700; }}
.step2-crop-group {{ margin-top: 8px; }}
.step2-crop-group h3 {{ margin: 0 0 6px; color: #596575; font-size: 13px; }}
.step2-crop-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px; align-items: start; }}
.step2-crop-card {{ margin: 0; border: 1px solid #dce3ec; border-radius: 4px; background: #fff; padding: 5px; overflow: auto; }}
.step2-crop-card img {{ display: block; width: 100%; height: auto; max-height: 420px; object-fit: contain; }}
.table-wrap {{ overflow-x: auto; margin: 8px 0 10px; }}
.html-table {{ border-collapse: collapse; margin: 0 auto; background: #fff; }}
.html-table td,
.html-table th {{ border: 1px solid #2b3340; padding: 5px 9px; text-align: center; line-height: 1.45; }}
.html-table th {{ font-weight: 700; background: #f5f8fb; }}
.qa-section {{ margin-top: 12px; padding-top: 10px; border-top: 1px solid #e1e7ef; }}
.qa-section h3 {{ margin: 0 0 6px; font-size: 15px; color: #165e96; }}
.qa-section summary {{ cursor: pointer; color: #165e96; font-weight: 700; }}
.answer-section {{ border-top-color: #cfe2d6; }}
.answer-section .text-block {{ color: #173f2a; font-weight: 600; }}
.analysis-section .qa-body,
.rubric-section .qa-body {{ margin-top: 8px; }}
.issues {{ margin-top: 10px; color: #5d6878; font-size: 13px; }}
.issues summary {{ cursor: pointer; }}
.empty {{ color: #7a8494; }}
.missing-asset {{ border: 1px dashed #ba5c5c; color: #9a3d3d; padding: 2px 5px; border-radius: 4px; }}
mjx-container[jax="CHTML"][display="true"] {{ margin: .45em 0; }}
@media (max-width: 760px) {{ main {{ padding: 10px; }} .options {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header>
<h1>{esc(run_id)} MathJax Web Review</h1>
<nav>{links}</nav>
</header>
<main>
{questions}
</main>
</body>
</html>
"""
    write_text(out_path, page)
    return out_path


def discover_run_ids(qb_root: Path) -> list[str]:
    return sorted(path.name for path in qb_root.iterdir() if (path / "question_bank.json").exists())


def discover_rendered_run_ids(output_root: Path) -> list[str]:
    if not output_root.exists():
        return []
    return sorted(path.name for path in output_root.iterdir() if (path / "index.html").exists())


def render_index(run_ids: list[str], output_root: Path) -> Path:
    rows = []
    for run_id in run_ids:
        rows.append(f'<li><a href="{esc(run_id)}/index.html">{esc(run_id)}</a></li>')
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Question Bank MathJax Review</title>
<style>
body {{ margin: 28px; background: #f5f6f8; color: #17202a; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; background: white; border: 1px solid #d9dee7; border-radius: 6px; padding: 20px 24px; }}
h1 {{ margin-top: 0; font-size: 22px; }}
li {{ margin: 10px 0; }}
a {{ color: #165e96; text-decoration: none; font-weight: 600; }}
p {{ color: #596575; line-height: 1.6; }}
</style>
</head>
<body>
<main>
<h1>Question Bank MathJax Review</h1>
<p>Browser-based rendering using MathJax. It keeps TeX math intact and avoids PDF-renderer escaping of alignment characters.</p>
<ul>
{''.join(rows)}
</ul>
</main>
</body>
</html>
"""
    out_path = output_root / "index.html"
    write_text(out_path, page)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render question-bank JSON to MathJax HTML review pages.")
    parser.add_argument("--run-id", action="append", help="Run id to render. Repeatable; defaults to all.")
    parser.add_argument("--qb-root", type=Path, default=DEFAULT_QB_ROOT)
    parser.add_argument("--asset-root", type=Path, default=DEFAULT_ASSET_ROOT)
    parser.add_argument("--block-root", type=Path, default=DEFAULT_BLOCK_ROOT)
    parser.add_argument("--source-runs", type=Path, default=DEFAULT_SOURCE_RUNS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--no-asset-sync", action="store_true", help="Do not copy image assets into the MathJax output directory.")
    args = parser.parse_args()

    args.qb_root = args.qb_root.resolve()
    args.asset_root = args.asset_root.resolve()
    args.block_root = args.block_root.resolve()
    args.source_runs = args.source_runs.resolve()
    args.output_root = args.output_root.resolve()
    run_ids = args.run_id or discover_run_ids(args.qb_root)
    outputs = [
        render_run(
            run_id,
            args.qb_root,
            args.asset_root,
            args.output_root,
            args.block_root,
            args.source_runs,
            asset_sync=not args.no_asset_sync,
        )
        for run_id in run_ids
    ]
    index_run_ids = sorted(set(run_ids) | set(discover_rendered_run_ids(args.output_root)))
    index = render_index(index_run_ids, args.output_root)
    print(json.dumps({"index": str(index), "runs": [str(path) for path in outputs]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
