from __future__ import annotations

import re
from typing import Any


def unique_str(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def asset_kind(block: dict[str, Any]) -> str | None:
    label = str(block.get("label") or "")
    local = label.split("-")[-1]
    block_type = str(block.get("type") or "").lower()
    if "caption" in block_type or "footnote" in block_type:
        return None
    if "table" in block_type:
        return "table"
    if "chart" in block_type:
        return "chart"
    if "image" in block_type or block.get("figure_id") or local.startswith("P"):
        return "image"
    return None


def packet_blocks(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for packet in packets:
        for order, block in enumerate(packet.get("blocks") or []):
            if not block.get("label"):
                continue
            row = dict(block)
            row["_packet_page"] = int(packet.get("page") or 0)
            row["_packet_order"] = order
            blocks.append(row)
    blocks.sort(key=lambda item: (int(item.get("_packet_page") or 0), int(item.get("_packet_order") or 0)))
    return blocks


def labels_in_range(range_item: dict[str, Any], packets: list[dict[str, Any]]) -> list[str]:
    blocks = packet_blocks(packets)
    index = {str(block.get("label")): idx for idx, block in enumerate(blocks)}
    start = index.get(str(range_item.get("start_label") or ""))
    end = index.get(str(range_item.get("end_label") or ""))
    if start is None or end is None or end < start:
        return []
    return [str(block.get("label")) for block in blocks[start : end + 1] if block.get("label")]


def has_embedded_question_marker(text: str, qno: int) -> bool:
    compact = re.sub(r"\s+", "", text)
    return any(f"{qno}{mark}" in compact for mark in ("、", ".", "．"))


def labels_in_range_with_shared_start(range_item: dict[str, Any], packets: list[dict[str, Any]]) -> list[str]:
    labels = labels_in_range(range_item, packets)
    if not labels or range_item.get("question_no") is None:
        return labels
    blocks = packet_blocks(packets)
    index = {str(block.get("label")): idx for idx, block in enumerate(blocks)}
    start = index.get(str(range_item.get("start_label") or ""))
    if start is None or start <= 0:
        return labels
    try:
        qno = int(range_item["question_no"])
    except (TypeError, ValueError):
        return labels
    previous = blocks[start - 1]
    previous_label = str(previous.get("label") or "")
    if previous_label and has_embedded_question_marker(str(previous.get("text") or ""), qno):
        return unique_str([previous_label] + labels)
    return labels


def label_to_q(label: str) -> str:
    return re.sub(r"^[A-Z]-", "Q-", label)


def label_to_a(label: str) -> str:
    return re.sub(r"^[A-Z]-", "A-", label)


def block_by_label(packets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for packet in packets:
        page = int(packet.get("page") or 0)
        for order, block in enumerate(packet.get("blocks") or []):
            if not block.get("label"):
                continue
            row = dict(block)
            row.setdefault("page", page)
            row["_packet_page"] = page
            row["_packet_order"] = order
            out[str(block.get("label"))] = row
    return out


def rule_visual_assets(question_labels: list[str], blocks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    asset_blocks = [blocks[label] for label in question_labels if label in blocks and asset_kind(blocks[label])]
    if not asset_blocks:
        return assets
    option_asset_mode = len(asset_blocks) == 4
    option_labels = ["A", "B", "C", "D"]
    ordered = sorted(
        asset_blocks,
        key=lambda block: (
            int(block.get("page") or block.get("_packet_page") or 0),
            round(float((block.get("bbox") or [0, 0, 0, 0])[1]) / 80),
            float((block.get("bbox") or [0, 0, 0, 0])[0]),
        ),
    )
    for idx, block in enumerate(ordered):
        kind = asset_kind(block) or "image"
        label = str(block.get("label"))
        if option_asset_mode:
            role = "option_table" if kind == "table" else "option_figure"
            option_label = option_labels[idx]
        else:
            role = "stem_table" if kind == "table" else "stem_figure"
            option_label = None
        assets.append(
            {
                "label": label,
                "kind": kind,
                "role": role,
                "option_label": option_label,
                "caption_labels": [],
                "caption_text": "",
                "reason": "v11 range prefill from block range; Step4 asset matcher may refine.",
            }
        )
    return assets
