from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from typing import Any

from exam_import.core.io import write_json, write_text
from exam_import.core.run_context import RunContext
from exam_import.schemas.question_record import QuestionRecord
from exam_import.sources.ocr_blocks import load_ocr_payload, load_packets


PLACEHOLDER_RE = re.compile(r"<(img|table|chart)\s+src=\"([^\"]+)\">")
VISUAL_KINDS = {"image", "table", "chart"}


@dataclass(frozen=True)
class SourceAssetRef:
    label: str
    kind: str
    source_part: str
    block_id: str
    figure_id: str


def export_render_assets(
    *,
    run_context: RunContext,
    records: list[QuestionRecord],
    asset_root: Path,
) -> dict[str, Any]:
    if asset_root.exists():
        shutil.rmtree(asset_root)
    asset_root.mkdir(parents=True, exist_ok=True)

    label_usage = _collect_asset_usage(records)
    source_index = _build_source_asset_index(run_context)
    block_cache: dict[str, dict[str, dict[str, Any]]] = {}
    manifest_entries: list[dict[str, Any]] = []
    exported_count = 0
    missing_count = 0
    table_html_count = 0
    raster_copy_count = 0

    for label in sorted(label_usage):
        used_tags = sorted(label_usage[label])
        ref = source_index.get(label)
        if ref is None:
            missing_count += 1
            manifest_entries.append(
                {
                    "label": label,
                    "used_tags": used_tags,
                    "status": "missing_label_mapping",
                    "source_kind": "unknown",
                    "source_part": "unknown",
                    "exported_files": [],
                }
            )
            continue

        part_dir = run_context.source_run_dir / ref.source_part
        if not part_dir.exists():
            missing_count += 1
            manifest_entries.append(
                {
                    "label": label,
                    "used_tags": used_tags,
                    "status": "missing_source_part",
                    "source_kind": ref.kind,
                    "source_part": ref.source_part,
                    "exported_files": [],
                }
            )
            continue

        block = _resolve_source_block(
            part_dir=part_dir,
            block_id=ref.block_id,
            figure_id=ref.figure_id,
            cache=block_cache,
        )
        if block is None:
            missing_count += 1
            manifest_entries.append(
                {
                    "label": label,
                    "used_tags": used_tags,
                    "status": "missing_source_block",
                    "source_kind": ref.kind,
                    "source_part": ref.source_part,
                    "exported_files": [],
                }
            )
            continue

        exported_files: list[str] = []
        table_html = _extract_table_html(block)
        if ref.kind == "table" and table_html:
            html_path = asset_root / f"{label}.html"
            write_text(html_path, table_html)
            exported_files.append(html_path.name)
            table_html_count += 1

        source_image = _resolve_source_image_path(part_dir, block)
        if source_image is not None:
            raster_path = asset_root / f"{label}{source_image.suffix.lower()}"
            shutil.copy2(source_image, raster_path)
            exported_files.append(raster_path.name)
            raster_copy_count += 1

        if exported_files:
            exported_count += 1
            status = "exported"
        else:
            missing_count += 1
            status = "missing_source_asset"
        manifest_entries.append(
            {
                "label": label,
                "used_tags": used_tags,
                "status": status,
                "source_kind": ref.kind,
                "source_part": ref.source_part,
                "exported_files": exported_files,
            }
        )

    summary = {
        "run_id": run_context.run_id,
        "alignment_mode": run_context.alignment_mode,
        "asset_reference_count": len(label_usage),
        "exported_asset_count": exported_count,
        "missing_asset_count": missing_count,
        "table_html_count": table_html_count,
        "raster_copy_count": raster_copy_count,
        "entries": manifest_entries,
    }
    write_json(run_context.render_dir / "assets_manifest.json", summary)
    return summary


def _collect_asset_usage(records: list[QuestionRecord]) -> dict[str, set[str]]:
    usage: dict[str, set[str]] = {}
    for record in records:
        segments = list(record.stem_markdown)
        segments.extend(record.answer_markdown)
        segments.extend(record.analysis_markdown)
        for group in record.options_markdown:
            for option in group.options:
                segments.extend(option.content_markdown)
        for segment in segments:
            for match in PLACEHOLDER_RE.finditer(segment):
                tag = match.group(1)
                label = match.group(2).strip()
                if not label:
                    continue
                usage.setdefault(label, set()).add(tag)
    return usage


def _build_source_asset_index(run_context: RunContext) -> dict[str, SourceAssetRef]:
    index: dict[str, SourceAssetRef] = {}
    packet_plans: list[tuple[Path, str]] = []
    question_packets_dir = run_context.step2_run_dir / "question_packets"
    if question_packets_dir.exists():
        question_part = "mixed" if run_context.alignment_mode == "mixed" else "paper"
        packet_plans.append((question_packets_dir, question_part))
    answer_packets_dir = run_context.step2_run_dir / "answer_packets"
    if answer_packets_dir.exists():
        packet_plans.append((answer_packets_dir, "answer"))

    for packet_dir, source_part in packet_plans:
        for packet in load_packets(packet_dir):
            for block in packet.get("blocks") or []:
                kind = str(block.get("kind") or block.get("type") or "").lower()
                if kind not in VISUAL_KINDS:
                    continue
                label = str(block.get("label") or "").strip()
                if not label:
                    continue
                ref = SourceAssetRef(
                    label=label,
                    kind=kind,
                    source_part=source_part,
                    block_id=str(block.get("block_id") or ""),
                    figure_id=str(block.get("figure_id") or ""),
                )
                previous = index.get(label)
                if previous is not None and previous != ref:
                    raise RuntimeError(f"Conflicting Step5 asset label mapping for {label}")
                index[label] = ref
    return index


def _resolve_source_block(
    *,
    part_dir: Path,
    block_id: str,
    figure_id: str,
    cache: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any] | None:
    key = str(part_dir.resolve())
    if key not in cache:
        payload = load_ocr_payload(part_dir)
        by_block: dict[str, dict[str, Any]] = {}
        by_figure: dict[str, dict[str, Any]] = {}
        for block in payload.get("blocks") or []:
            block_key = str(block.get("block_id") or "")
            figure_key = str(block.get("figure_id") or "")
            if block_key and block_key not in by_block:
                by_block[block_key] = block
            if figure_key and figure_key not in by_figure:
                by_figure[figure_key] = block
        cache[key] = {"block": by_block, "figure": by_figure}
    indexes = cache[key]
    if block_id and block_id in indexes["block"]:
        return indexes["block"][block_id]
    if figure_id and figure_id in indexes["figure"]:
        return indexes["figure"][figure_id]
    return None


def _extract_table_html(block: dict[str, Any]) -> str:
    mineru_item = block.get("mineru_item") or {}
    table_body = str(mineru_item.get("table_body") or "").strip()
    if table_body:
        return table_body
    text = str(block.get("text") or "").strip()
    if text.startswith("<table"):
        return text
    return ""


def _resolve_source_image_path(part_dir: Path, block: dict[str, Any]) -> Path | None:
    mineru_item = block.get("mineru_item") or {}
    candidates: list[Path] = []
    for path_text in (
        str(block.get("path") or "").strip(),
        str(mineru_item.get("img_path") or "").strip(),
    ):
        if not path_text:
            continue
        normalized = Path(path_text.replace("\\", "/"))
        if normalized.is_absolute():
            candidates.append(normalized)
        else:
            candidates.append(part_dir / normalized)
            candidates.append(part_dir / "mineru_extract" / normalized)
            candidates.append(part_dir / "mineru_extract" / "images" / normalized.name)
            candidates.append(part_dir / "images" / normalized.name)
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    return None
