from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKTREE_ROOT = Path(__file__).resolve().parent
CODE_ROOT = WORKTREE_ROOT.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import step3_question2json as standardize  # noqa: E402
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
from common_io import data_uri, read_json, text_excerpt, write_json, write_jsonl  # noqa: E402
from common_llm import call_chat_json, configure_llm_provider_env  # noqa: E402
from common_token_budget import DEFAULT_OUTPUT_RESERVE_TOKENS, configure_token_budget_env  # noqa: E402


DEFAULT_MODEL = "Qwen/Qwen3.6-35B-A3B"
DEFAULT_LLM_PROVIDER = "siliconflow"
DEFAULT_OUTPUT_ROOT = WORKTREE_ROOT / "runs_step2_exam_blocks"
DEFAULT_QB_ROOT = WORKTREE_ROOT / "runs_question_bank"
DEFAULT_REVIEW_ROOT = WORKTREE_ROOT / "reviews_question_bank"
DEFAULT_RENDER_ROOT = WORKTREE_ROOT / "rendered_question_bank_mathjax"
DEFAULT_STEP3_PACKET_REVIEW_ROOT = WORKTREE_ROOT / "reviews_step3_packets"

TOP_LEVEL_PREFIX_RE = re.compile(r"^\s*(\d{1,2})\s*[.．、，,)]")
TABLE_CELL_NUMBER_RE = re.compile(r"<td>\s*(\d{1,2})\s*</td>")


VISUAL_ASSET_ROLES = {
    "stem_figure",
    "option_figure",
    "stem_table",
    "option_table",
    "answer_figure",
    "analysis_figure",
    "rubric_table",
    "noise",
    "uncertain",
}
QUESTION_SIDE_ASSET_ROLES = {"stem_figure", "option_figure", "stem_table", "option_table"}
ANSWER_SIDE_ASSET_ROLES = {"answer_figure", "analysis_figure", "rubric_table"}
VISUAL_ASSIGNMENT_PAGE_WORKERS = 4
ANSWER_TABLE_PAGE_WORKERS = 4


def normalize_worker_models(primary_model: str, worker_models: list[str] | None) -> list[str]:
    models = [str(model).strip() for model in (worker_models or []) if str(model).strip()]
    if not models:
        models = [primary_model]
    return list(dict.fromkeys(models))


def multi_model_name(worker_models: list[str]) -> str:
    if len(worker_models) == 1:
        return worker_models[0]
    return "multi_model[" + ",".join(worker_models) + "]"


def assign_context_models(contexts: list[dict[str, Any]], worker_models: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not contexts:
        return [], []
    weights = [len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) for context in contexts]
    totals = {model: 0 for model in worker_models}
    partitions = {model: {"model": model, "pages": [], "weight": 0, "count": 0} for model in worker_models}
    assigned: list[dict[str, Any]] = [dict(context) for context in contexts]
    indexed = sorted(range(len(contexts)), key=lambda index: (-weights[index], str(contexts[index].get("stream") or ""), int(contexts[index].get("page") or 0)))
    model_order = {model: index for index, model in enumerate(worker_models)}
    for index in indexed:
        model = min(worker_models, key=lambda item: (totals[item], partitions[item]["count"], model_order[item]))
        assigned[index]["_worker_model"] = model
        assigned[index]["_worker_weight"] = weights[index]
        totals[model] += weights[index]
        partitions[model]["weight"] = totals[model]
        partitions[model]["count"] = int(partitions[model]["count"]) + 1
        partitions[model]["pages"].append(
            {
                "stream": contexts[index].get("stream"),
                "page": contexts[index].get("page"),
            }
        )
    return assigned, [partitions[model] for model in worker_models if partitions[model]["count"]]


def visual_role_for_hint(kind: str | None, hint: str | None) -> str:
    kind = str(kind or "image").lower()
    hint = str(hint or "unknown").lower()
    table = kind == "table"
    if hint == "option":
        return "option_table" if table else "option_figure"
    if hint == "answer":
        return "answer_figure"
    if hint == "analysis":
        return "analysis_figure"
    if hint == "rubric":
        return "rubric_table" if table else "analysis_figure"
    return "stem_table" if table else "stem_figure"


def normalize_step3_role_match_text(text: Any) -> str:
    value = str(text or "")
    value = value.replace("\\because", "∵").replace("\\therefore", "∴")
    value = value.replace("\\left", "").replace("\\right", "")
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[$`]", "", value)
    value = re.sub(r"[，。；：、,.!?！？（）()\[\]【】“”\"'<>]", "", value)
    value = re.sub(r"[{}]", "", value)
    return value.lower()


def best_step3_role_match(text: str, role_texts: dict[str, list[str]]) -> dict[str, Any] | None:
    needle = normalize_step3_role_match_text(text)
    if len(needle) < 8:
        return None
    best: dict[str, Any] | None = None
    for role in ("stem", "answer", "analysis", "rubric"):
        for candidate in role_texts.get(role) or []:
            haystack = normalize_step3_role_match_text(candidate)
            if len(haystack) < 8:
                continue
            if needle in haystack or haystack in needle:
                score = 1.0
            else:
                score = SequenceMatcher(None, needle[:260], haystack[:260]).ratio()
            if best is None or score > float(best["score"]):
                best = {
                    "role": role,
                    "score": round(score, 3),
                    "evidence": text_excerpt(str(candidate), limit=180),
                }
    if best and float(best["score"]) >= 0.58:
        return best
    return None


def row_visual_label_meta(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}

    def put(label: str, qno: int, hint: str) -> None:
        if not label:
            return
        row = {"q": qno, "hint": hint}
        role_texts = role_texts_by_qno.get(qno)
        if role_texts:
            row["step3_role_texts"] = role_texts
        meta.setdefault(label, row)
        meta.setdefault(label_to_q(label), row)
        meta.setdefault(label_to_a(label), row)

    role_texts_by_qno: dict[int, dict[str, list[str]]] = {}
    for row in rows:
        try:
            qno = int(row.get("question_no"))
        except (TypeError, ValueError):
            continue
        role_texts = row.get("_standardized_role_texts")
        if isinstance(role_texts, dict):
            role_texts_by_qno[qno] = {
                str(role): [str(item) for item in values or [] if str(item).strip()]
                for role, values in role_texts.items()
                if isinstance(values, list)
            }

    for row in rows:
        try:
            qno = int(row.get("question_no"))
        except (TypeError, ValueError):
            continue
        for label in row.get("question_labels") or []:
            put(str(label), qno, "stem")
        for item in row.get("answer_items") or []:
            role = str(item.get("role") or "answer").lower()
            hint = role if role in {"answer", "analysis", "rubric"} else "answer"
            for label in item.get("block_labels") or []:
                put(str(label), qno, hint)
    return meta


def rule_asset_prefill_by_label(question_groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    prefill: dict[str, dict[str, Any]] = {}
    for group in question_groups:
        try:
            qno = int(group.get("question_no"))
        except (TypeError, ValueError):
            continue
        for asset in group.get("visual_assets") or []:
            if not isinstance(asset, dict) or not asset.get("label"):
                continue
            item = {
                "question_no": qno,
                "role": str(asset.get("role") or ""),
                "option_label": asset.get("option_label"),
            }
            label = str(asset["label"])
            prefill[label] = item
            prefill[label_to_q(label)] = item
            prefill[label_to_a(label)] = item
    return prefill


def bbox_center_y(block: dict[str, Any]) -> float:
    box = block.get("bbox") or [0, 0, 0, 0]
    try:
        return (float(box[1]) + float(box[3])) / 2
    except (TypeError, ValueError, IndexError):
        return 0.0


def bbox_top_y(block: dict[str, Any]) -> float:
    box = block.get("bbox") or [0, 0, 0, 0]
    try:
        return float(box[1])
    except (TypeError, ValueError, IndexError):
        return 0.0


def bbox_bottom_y(block: dict[str, Any]) -> float:
    box = block.get("bbox") or [0, 0, 0, 0]
    try:
        return float(box[3])
    except (TypeError, ValueError, IndexError):
        return 0.0


def page_extents_for_packets(packets: list[dict[str, Any]]) -> dict[int, float]:
    extents: dict[int, float] = {}
    for packet in packets:
        page = int(packet.get("page") or 0)
        max_y = 0.0
        for block in packet.get("blocks") or []:
            box = block.get("bbox") or []
            if len(box) >= 4:
                try:
                    max_y = max(max_y, float(box[3]))
                except (TypeError, ValueError):
                    pass
        extents[page] = max(max_y + 80.0, 1000.0)
    return extents


def page_offsets_for_packets(packets: list[dict[str, Any]], page_gap: float = 80.0) -> dict[int, float]:
    extents = page_extents_for_packets(packets)
    offsets: dict[int, float] = {}
    cursor = 0.0
    for page in sorted(extents):
        offsets[page] = cursor
        cursor += extents[page] + page_gap
    return offsets


def packet_global_positions(packets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    offsets = page_offsets_for_packets(packets)
    positions: dict[str, dict[str, Any]] = {}
    for packet in packets:
        page = int(packet.get("page") or 0)
        offset = offsets.get(page, 0.0)
        for order, block in enumerate(packet.get("blocks") or []):
            label = str(block.get("label") or "")
            if not label:
                continue
            row = dict(block)
            row.setdefault("page", page)
            row["_packet_order"] = order
            row["_global_y"] = offset + bbox_center_y(row)
            positions[label] = row
    return positions


def page_text_extents_for_positions(positions: dict[str, dict[str, Any]]) -> dict[int, tuple[float, float]]:
    extents: dict[int, tuple[float, float]] = {}
    for block in positions.values():
        if asset_kind(block):
            continue
        text = re.sub(r"\s+", " ", str(block.get("text") or "")).strip()
        if not text:
            continue
        page = int(block.get("page") or 0)
        top = bbox_top_y(block)
        bottom = bbox_bottom_y(block)
        if page not in extents:
            extents[page] = (top, bottom)
        else:
            old_top, old_bottom = extents[page]
            extents[page] = (min(old_top, top), max(old_bottom, bottom))
    return extents


def visual_text_reading_distance(
    asset: dict[str, Any],
    block: dict[str, Any],
    page_extents: dict[int, tuple[float, float]],
) -> tuple[float, str]:
    asset_page = int(asset.get("page") or 0)
    block_page = int(block.get("page") or 0)
    asset_top = bbox_top_y(asset)
    asset_bottom = bbox_bottom_y(asset)
    block_top = bbox_top_y(block)
    block_bottom = bbox_bottom_y(block)
    if asset_page == block_page:
        if block_bottom <= asset_top:
            return max(0.0, asset_top - block_bottom), "before"
        if block_top >= asset_bottom:
            return max(0.0, block_top - asset_bottom), "after"
        return 0.0, "overlap"

    def span(page: int) -> tuple[float, float]:
        return page_extents.get(page, (0.0, 1000.0))

    if block_page > asset_page:
        _asset_page_top, asset_page_bottom = span(asset_page)
        distance = max(0.0, asset_page_bottom - asset_bottom)
        for page in range(asset_page + 1, block_page):
            top, bottom = span(page)
            distance += max(0.0, bottom - top)
        block_page_top, _block_page_bottom = span(block_page)
        distance += max(0.0, block_top - block_page_top)
        return distance, "after"

    asset_page_top, _asset_page_bottom = span(asset_page)
    distance = max(0.0, asset_top - asset_page_top)
    for page in range(block_page + 1, asset_page):
        top, bottom = span(page)
        distance += max(0.0, bottom - top)
    _block_page_top, block_page_bottom = span(block_page)
    distance += max(0.0, block_page_bottom - block_bottom)
    return distance, "before"


def candidate_questions_for_visual_asset(
    asset: dict[str, Any],
    positions: dict[str, dict[str, Any]],
    meta: dict[str, dict[str, Any]],
    limit: int = 6,
) -> list[dict[str, Any]]:
    page_extents = page_text_extents_for_positions(positions)
    by_qno: dict[int, dict[str, Any]] = {}
    hint_priority = {"stem": 0, "option": 0, "answer": 0, "analysis": 0, "rubric": 0}
    for label, item in meta.items():
        block = positions.get(label)
        if not block or label == str(asset.get("label") or "") or asset_kind(block):
            continue
        text = re.sub(r"\s+", " ", str(block.get("text") or "")).strip()
        if not text:
            continue
        try:
            qno = int(item.get("q"))
        except (TypeError, ValueError):
            continue
        range_hint = str(item.get("hint") or "unknown")
        role_match = best_step3_role_match(text, item.get("step3_role_texts") or {})
        hint = str((role_match or {}).get("role") or range_hint)
        distance, side = visual_text_reading_distance(asset, block, page_extents)
        priority = hint_priority.get(hint, 9)
        state = by_qno.setdefault(
            qno,
            {
                "distance": distance,
                "nearest_label": label,
                "nearest_hint": hint,
                "nearest_side": side,
                "ref_priority": priority,
                "ref_distance": distance,
                "ref_label": label,
                "ref_hint": hint,
                "ref_range_hint": range_hint,
                "ref_step3_role": (role_match or {}).get("role"),
                "ref_step3_role_score": (role_match or {}).get("score"),
                "ref_step3_role_evidence": (role_match or {}).get("evidence"),
                "ref_text": text_excerpt(text, limit=180),
            },
        )
        if distance < float(state["distance"]):
            state["distance"] = distance
            state["nearest_label"] = label
            state["nearest_hint"] = hint
            state["nearest_side"] = side
        if distance < float(state["ref_distance"]) or (
            distance == float(state["ref_distance"]) and priority < int(state["ref_priority"])
        ):
            state["ref_priority"] = priority
            state["ref_distance"] = distance
            state["ref_label"] = label
            state["ref_hint"] = hint
            state["ref_range_hint"] = range_hint
            state["ref_step3_role"] = (role_match or {}).get("role")
            state["ref_step3_role_score"] = (role_match or {}).get("score")
            state["ref_step3_role_evidence"] = (role_match or {}).get("evidence")
            state["ref_text"] = text_excerpt(text, limit=180)
    out: list[dict[str, Any]] = []
    for rank, (qno, state) in enumerate(
        sorted(by_qno.items(), key=lambda pair: float(pair[1]["distance"]))[:limit],
        start=1,
    ):
        out.append(
            {
                "q": qno,
                "rank": rank,
                "label": state["ref_label"],
                "hint": state["ref_hint"],
                "range_hint": state["ref_range_hint"],
                "step3_role": state["ref_step3_role"],
                "step3_role_score": state["ref_step3_role_score"],
                "step3_role_evidence": state["ref_step3_role_evidence"],
                "nearest_label": state["nearest_label"],
                "nearest_hint": state["nearest_hint"],
                "side": state["nearest_side"],
                "text": state["ref_text"],
            }
        )
    return out


def allowed_roles_for_asset_part(pipeline_mode: str, stream: str) -> set[str]:
    if pipeline_mode == "mixed":
        return set(VISUAL_ASSET_ROLES)
    if stream == "answer":
        return set(ANSWER_SIDE_ASSET_ROLES) | {"noise", "uncertain"}
    return set(QUESTION_SIDE_ASSET_ROLES) | {"noise", "uncertain"}


def groups_for_stream(rows: list[dict[str, Any]], stream: str, question_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if stream != "answer":
        return question_groups
    groups: list[dict[str, Any]] = []
    for row in rows:
        try:
            qno = int(row.get("question_no"))
        except (TypeError, ValueError):
            continue
        labels: list[str] = []
        for item in row.get("answer_items") or []:
            if not isinstance(item, dict):
                continue
            labels.extend(str(label) for label in item.get("block_labels") or [] if str(label))
        if labels:
            groups.append({"question_no": qno, "block_labels": unique_str(labels), "visual_assets": [], "source": "answer_part"})
    return groups


def block_order_index(packets: list[dict[str, Any]]) -> dict[str, int]:
    order_by_label: dict[str, int] = {}
    cursor = 0
    for packet in packets:
        for block in packet.get("blocks") or []:
            label = str(block.get("label") or "")
            if label:
                order_by_label[label] = cursor
                cursor += 1
    return order_by_label


def mineru_group_candidates(
    asset_label: str,
    packets: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    row_by_qno: dict[int, dict[str, Any]],
    limit: int = 6,
) -> list[dict[str, Any]]:
    order_by_label = block_order_index(packets)
    asset_order = order_by_label.get(asset_label)
    if asset_order is None:
        return []

    ranked: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        try:
            qno = int(group.get("question_no"))
        except (TypeError, ValueError):
            continue
        if qno not in row_by_qno:
            continue
        labels = [str(label) for label in group.get("block_labels") or [] if str(label)]
        visual_labels = [
            str(item.get("label"))
            for item in group.get("visual_assets") or []
            if isinstance(item, dict) and item.get("label")
        ]
        all_labels = labels + visual_labels
        orders = [order_by_label[label] for label in all_labels if label in order_by_label]
        if not orders:
            continue
        start_order = min(orders)
        end_order = max(orders)
        in_range = start_order <= asset_order <= end_order or asset_label in all_labels
        distance = 0 if in_range else min(abs(asset_order - start_order), abs(asset_order - end_order))
        source = "range_contains_asset" if asset_label in all_labels else ("range_span" if in_range else "nearest_range")
        ranked.append(
            {
                "q": qno,
                "rank": 0,
                "label": asset_label,
                "hint": "mineru_reading_order",
                "range_hint": source,
                "nearest_label": labels[0] if labels else "",
                "nearest_hint": "mineru_reading_order",
                "side": "inside" if in_range else "near",
                "text": "",
                "distance": distance,
                "start_order": start_order,
                "end_order": end_order,
                "labels": labels[:8],
                "visual_labels": visual_labels,
            }
        )

    ranked.sort(
        key=lambda item: (
            0 if item["range_hint"] == "range_contains_asset" else 1,
            int(item["distance"]),
            int(item["q"]),
        )
    )
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for rank, item in enumerate(ranked, start=1):
        qno = int(item["q"])
        if qno in seen:
            continue
        seen.add(qno)
        candidate = dict(item)
        candidate["rank"] = rank
        row = row_by_qno.get(qno, {})
        if row.get("standardized_text"):
            candidate["standardized_text"] = row["standardized_text"]
        out.append(candidate)
        if len(out) >= limit:
            break
    return out


def visual_assignment_context_for_packet(
    packet: dict[str, Any],
    packets: list[dict[str, Any]],
    stream: str,
    pipeline_mode: str,
    rows: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    page = int(packet.get("page") or 0)
    assets = [
        dict(block)
        for block in packet.get("blocks") or []
        if block.get("label") and asset_kind(block) in {"image", "chart", "table"}
    ]
    if not assets:
        return None
    positions = packet_global_positions(packets)
    prefill = rule_asset_prefill_by_label(groups)
    allowed_roles = allowed_roles_for_asset_part(pipeline_mode, stream)
    row_by_qno = {
        int(row["question_no"]): row
        for row in rows
        if row.get("question_no") is not None
    }
    contexts: list[dict[str, Any]] = []
    for asset in assets:
        label = str(asset.get("label") or "")
        asset = positions.get(label, asset)
        candidate_questions = mineru_group_candidates(label, packets, groups, row_by_qno)
        candidate_qnos = [int(item["q"]) for item in candidate_questions if item.get("q") is not None]
        rule = prefill.get(label) or {}
        rule_qno = rule.get("question_no")
        rule_role = str(rule.get("role") or "")
        if not rule_role and candidate_questions:
            rule_role = visual_role_for_hint(asset_kind(asset), str(candidate_questions[0].get("hint") or "unknown"))
        if rule_role and rule_role not in allowed_roles:
            rule_role = ""
        step3_rule_role = None
        rule_role_source = "range_prefill"
        if candidate_questions:
            top_hint = str(candidate_questions[0].get("step3_role") or candidate_questions[0].get("hint") or "unknown")
            inferred = visual_role_for_hint(asset_kind(asset), top_hint)
            if inferred in ANSWER_SIDE_ASSET_ROLES and inferred in allowed_roles:
                step3_rule_role = inferred
                rule_role = inferred
                rule_role_source = "step3_standardized_nearest_role"
        contexts.append(
            {
                "label": label,
                "kind": asset_kind(asset) or "image",
                "candidate_questions": candidate_questions,
                "candidate_qnos": candidate_qnos,
                "rule_qno": rule_qno,
                "rule_role": rule_role or None,
                "step3_rule_role": step3_rule_role,
                "rule_role_source": rule_role_source,
                "allowed_roles": sorted(allowed_roles),
                "asset_part": "mixed" if pipeline_mode == "mixed" else stream,
                "candidate_source": "mineru_reading_order",
            }
        )
    return {
        "stream": stream,
        "pipeline_mode": pipeline_mode,
        "page": page,
        "annotated_page_image": packet.get("annotated_page_image"),
        "assets": contexts,
    }


def build_visual_asset_assignment_messages(run_id: str, mode: str, page_context: dict[str, Any]) -> list[dict[str, Any]]:
    compact = {
        "run_id": run_id,
        "mode": mode,
        "stream": page_context.get("stream"),
        "page": page_context.get("page"),
        "assets": page_context.get("assets") or [],
    }
    instruction = (
        "/no_think\n"
        "输入是一页带框标注的中文数学试卷图片，以及本页图像、图表、表格资产的紧凑上下文。\n"
        "请把每个资产归属到一个顶层题号，并给出最终角色。候选题号来自 MinerU 阅读顺序范围，不能额外扩展。\n"
        "不要解题，不要把图片内容描述成题库正文。\n\n"
        "规则：\n"
        "- 除 noise 或 uncertain 外，question_no 必须来自该资产的 candidate_qnos。\n"
        "- 每个资产只能使用自身 allowed_roles 中列出的角色；paper 部分只允许题干/选项侧角色，answer 部分只允许答案/解析/评分侧角色，mixed 模式允许统一分配。\n"
        "- 若 rule_role_source 为 step3_standardized_nearest_role，可把 rule_role 作为默认角色；但仍不得超出 allowed_roles。\n"
        "- option_figure 和 option_table 必须填写 option_label=A/B/C/D。\n"
        "- 二维码、水印、广告、装饰图或无关资产使用 noise。\n"
        "- 无法安全判断时使用 uncertain。\n"
        "- caption_labels 只能填写附近作为图题、表题的文本块标签。\n"
        "- caption_text 只复制图题或表题原文，不要描述资产内容。\n\n"
        "只返回严格 JSON，格式如下：\n"
        '{"assets":[{"label":"Q-V08-P01","question_no":17,"role":"stem_figure","option_label":null,'
        '"caption_labels":[],"caption_text":"","confidence":0.0,"reason":"简短中文理由"}],'
        '"risks":[{"label":"Q-V08-P01","severity":"info|warning|error","reason":"简短中文风险"}]}\n\n'
        "紧凑上下文：\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
    )
    content: list[dict[str, Any]] = []
    image_path = Path(str(page_context.get("annotated_page_image") or ""))
    if not image_path.is_absolute():
        image_path = WORKTREE_ROOT / image_path
    if image_path.exists():
        content.append({"type": "text", "text": f"{page_context.get('stream')} 第 {page_context.get('page')} 页标注图："})
        content.append({"type": "image_url", "image_url": {"url": data_uri(image_path)}})
    content.append({"type": "text", "text": instruction})
    return [
        {"role": "system", "content": "你只做中文数学试卷图像、图表、表格资产归属，返回严格 JSON。"},
        {"role": "user", "content": content},
    ]


def normalize_visual_assignment_page(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    by_label = {str(item.get("label") or ""): item for item in payload.get("assets") or [] if isinstance(item, dict)}
    normalized: list[dict[str, Any]] = []
    for asset in context.get("assets") or []:
        label = str(asset.get("label") or "")
        item = by_label.get(label)
        if not item:
            raise ValueError(f"Step4 visual assignment omitted asset {label}")
        role = str(item.get("role") or "uncertain").strip()
        if role not in VISUAL_ASSET_ROLES:
            role = "uncertain"
        allowed_roles = set(str(value) for value in asset.get("allowed_roles") or VISUAL_ASSET_ROLES)
        if role not in allowed_roles:
            raise ValueError(f"Step4 visual assignment role {role} is outside allowed_roles for {label}: {sorted(allowed_roles)}")
        candidate_qnos = {int(qno) for qno in asset.get("candidate_qnos") or [] if str(qno).isdigit()}
        try:
            qno = int(item.get("question_no")) if item.get("question_no") is not None else None
        except (TypeError, ValueError):
            qno = None
        if role not in {"noise", "uncertain"} and qno not in candidate_qnos:
            qno = None
            role = "uncertain"
        step3_rule_role = str(asset.get("step3_rule_role") or "")
        role_source = str(asset.get("rule_role_source") or "")
        reason = str(item.get("reason") or "")
        if (
            step3_rule_role in ANSWER_SIDE_ASSET_ROLES
            and role in QUESTION_SIDE_ASSET_ROLES
            and qno in candidate_qnos
        ):
            if step3_rule_role not in allowed_roles:
                raise ValueError(f"Step4 Step3 role {step3_rule_role} is outside allowed_roles for {label}")
            role = step3_rule_role
            reason = (reason + " " if reason else "") + f"Step3 标准化归属将角色修正为 {step3_rule_role}。"
        option_label = item.get("option_label")
        if option_label is not None:
            option_label = str(option_label).strip().upper()
            if option_label in {"", "NONE", "NULL"}:
                option_label = None
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        normalized.append(
            {
                "label": label,
                "kind": asset.get("kind") or "image",
                "question_no": qno,
                "role": role,
                "option_label": option_label,
                "caption_labels": [str(value) for value in item.get("caption_labels") or []],
                "caption_text": str(item.get("caption_text") or ""),
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": reason,
                "candidate_qnos": asset.get("candidate_qnos") or [],
                "rule_qno": asset.get("rule_qno"),
                "rule_role": asset.get("rule_role"),
                "step3_rule_role": step3_rule_role or None,
                "rule_role_source": role_source or None,
                "allowed_roles": sorted(allowed_roles),
                "asset_part": asset.get("asset_part"),
                "candidate_source": asset.get("candidate_source"),
            }
        )
    risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
    return {"assets": normalized, "risks": risks}


def assign_visual_assets_with_llm(
    run_id: str,
    pipeline_mode: str,
    question_groups: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    question_packets: list[dict[str, Any]],
    answer_packets: list[dict[str, Any]],
    model: str,
    timeout: int,
    out_path: Path,
    force: bool,
    worker_models: list[str] | None = None,
) -> dict[str, Any]:
    if out_path.exists() and not force:
        return read_json(out_path, {})
    active_models = normalize_worker_models(model, worker_models)

    if pipeline_mode == "mixed":
        packet_sources: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = [
            ("mixed", question_packets, question_groups)
        ]
    else:
        packet_sources = [("question", question_packets, question_groups)]
        if pipeline_mode == "paper_plus_answer_file" and answer_packets:
            packet_sources.append(("answer", answer_packets, groups_for_stream(rows, "answer", question_groups)))

    page_contexts: list[dict[str, Any]] = []
    for stream, packets, groups in packet_sources:
        for packet in packets:
            context = visual_assignment_context_for_packet(
                packet=packet,
                packets=packets,
                stream=stream,
                pipeline_mode=pipeline_mode,
                rows=rows,
                groups=groups,
            )
            if context and context.get("assets"):
                page_contexts.append(context)

    page_contexts, worker_partitions = assign_context_models(page_contexts, active_models)
    write_json(out_path.with_name(out_path.stem + "_input_compact.json"), {"run_id": run_id, "mode": pipeline_mode, "worker_partitions": worker_partitions, "pages": page_contexts})
    if not page_contexts:
        review = {
            "run_id": run_id,
            "model": multi_model_name(active_models),
            "worker_models": active_models,
            "worker_partitions": [],
            "mode": pipeline_mode,
            "pages": [],
            "summary": {"asset_count": 0, "assigned_count": 0, "noise_count": 0, "uncertain_count": 0, "elapsed_seconds_sum": 0.0, "elapsed_seconds_max": 0.0},
        }
        write_json(out_path, review)
        return review

    def call_page(context: dict[str, Any]) -> dict[str, Any]:
        messages = build_visual_asset_assignment_messages(run_id, pipeline_mode, context)
        request_model = str(context.get("_worker_model") or model)
        parsed, raw, elapsed = call_chat_json(messages, model=request_model, timeout=timeout)
        normalized = normalize_visual_assignment_page(parsed, context)
        return {
            "stream": context.get("stream"),
            "page": context.get("page"),
            "model": request_model,
            "llm_elapsed_seconds": round(elapsed, 3),
            "assets": normalized["assets"],
            "risks": normalized["risks"],
            "raw_response": raw,
        }

    pages: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(VISUAL_ASSIGNMENT_PAGE_WORKERS, len(page_contexts))) as pool:
        future_map = {pool.submit(call_page, context): context for context in page_contexts}
        for future in as_completed(future_map):
            pages.append(future.result())
    pages.sort(key=lambda item: (str(item.get("stream") or ""), int(item.get("page") or 0)))
    all_assets = [asset for page in pages for asset in page.get("assets") or []]
    elapsed_values = [float(page.get("llm_elapsed_seconds") or 0.0) for page in pages]
    review = {
        "run_id": run_id,
        "model": multi_model_name(active_models),
        "worker_models": active_models,
        "worker_partitions": worker_partitions,
        "mode": pipeline_mode,
        "pages": pages,
        "summary": {
            "asset_count": len(all_assets),
            "assigned_count": sum(1 for asset in all_assets if asset.get("role") not in {"noise", "uncertain"}),
            "noise_count": sum(1 for asset in all_assets if asset.get("role") == "noise"),
            "uncertain_count": sum(1 for asset in all_assets if asset.get("role") == "uncertain"),
            "risk_count": sum(len(page.get("risks") or []) for page in pages),
            "elapsed_seconds_sum": round(sum(elapsed_values), 3),
            "elapsed_seconds_max": round(max(elapsed_values), 3) if elapsed_values else 0.0,
        },
    }
    write_json(out_path, review)
    return review


def normalize_short_answer_text(text: Any) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if re.fullmatch(r"[A-D]", value, re.IGNORECASE):
        return f"${value.upper()}$"
    return value


def answer_table_candidate_qnos(
    answer_packets: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    run_dir: Path,
) -> dict[str, list[int]]:
    out: dict[str, set[int]] = {}
    for row in rows:
        try:
            qno = int(row.get("question_no"))
        except (TypeError, ValueError):
            continue
        for item in row.get("answer_items") or []:
            for label in item.get("block_labels") or []:
                text = str(label)
                if text:
                    out.setdefault(text, set()).add(qno)

    answer_ranges = read_json(run_dir / "step2_pure_answer_ranges.json", {})
    for item in answer_ranges.get("question_ranges") or []:
        try:
            qno = int(item.get("question_no"))
        except (TypeError, ValueError):
            continue
        for label in labels_in_range(item, answer_packets):
            out.setdefault(str(label), set()).add(qno)
    return {label: sorted(qnos) for label, qnos in out.items()}


def answer_table_extraction_contexts(
    answer_packets: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    run_dir: Path,
) -> list[dict[str, Any]]:
    qnos_by_label = answer_table_candidate_qnos(answer_packets, rows, run_dir)
    contexts: list[dict[str, Any]] = []
    for packet in answer_packets:
        tables: list[dict[str, Any]] = []
        for block in packet.get("blocks") or []:
            if asset_kind(block) != "table" or not block.get("label"):
                continue
            label = str(block.get("label"))
            text = str(block.get("text") or "")
            tables.append(
                {
                    "label": label,
                    "candidate_qnos": qnos_by_label.get(label) or [],
                    "text": text_excerpt(text, limit=2200),
                }
            )
        if tables:
            contexts.append(
                {
                    "stream": "answer",
                    "page": int(packet.get("page") or 0),
                    "annotated_page_image": packet.get("annotated_page_image"),
                    "tables": tables,
                }
            )
    return contexts


def build_answer_table_extraction_messages(run_id: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    compact = {
        "run_id": run_id,
        "stream": context.get("stream"),
        "page": context.get("page"),
        "tables": context.get("tables") or [],
    }
    instruction = (
        "/no_think\n"
        "Input is one annotated answer page image plus OCR HTML for table blocks.\n"
        "Classify each table and, only when it is an answer-key table, extract per-question final answers.\n\n"
        "Rules:\n"
        "- Do not solve questions or infer answers not shown in the table.\n"
        "- If a table maps question numbers to choice letters/final answers, role is answer_key_table.\n"
        "- If a table is a scoring rubric, solution-process table, layout/noise, or cannot be used as final answers, return no entries.\n"
        "- For choice letters, answer_text must be LaTeX-wrapped like $A$.\n"
        "- question_no should come from the visible table and normally be in candidate_qnos.\n\n"
        "Return strict JSON only:\n"
        '{"tables":[{"label":"A-V01-B22","role":"answer_key_table|rubric_table|solution_table|noise|uncertain",'
        '"entries":[{"question_no":13,"answer_text":"$B$","confidence":0.0,"reason":"short"}],'
        '"confidence":0.0,"reason":"short"}],'
        '"risks":[{"label":"A-V01-B22","severity":"info|warning|error","reason":"short"}]}\n\n'
        "Compact context:\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
    )
    content: list[dict[str, Any]] = []
    image_path = Path(str(context.get("annotated_page_image") or ""))
    if not image_path.is_absolute():
        image_path = WORKTREE_ROOT / image_path
    if image_path.exists():
        content.append({"type": "text", "text": f"Annotated answer page image for page {context.get('page')}:"})
        content.append({"type": "image_url", "image_url": {"url": data_uri(image_path)}})
    content.append({"type": "text", "text": instruction})
    return [
        {"role": "system", "content": "Extract answer-key table entries from OCR exam answer pages. Return strict JSON."},
        {"role": "user", "content": content},
    ]


def normalize_answer_table_extraction_page(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    source_by_label = {str(table.get("label") or ""): table for table in context.get("tables") or []}
    by_label = {str(item.get("label") or ""): item for item in payload.get("tables") or [] if isinstance(item, dict)}
    normalized: list[dict[str, Any]] = []
    for label, source in source_by_label.items():
        item = by_label.get(label) or {}
        role = str(item.get("role") or "uncertain").strip()
        if role not in {"answer_key_table", "rubric_table", "solution_table", "noise", "uncertain"}:
            role = "uncertain"
        candidate_qnos = [int(qno) for qno in source.get("candidate_qnos") or [] if str(qno).isdigit()]
        entries: list[dict[str, Any]] = []
        if role == "answer_key_table":
            for entry in item.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    qno = int(entry.get("question_no"))
                except (TypeError, ValueError):
                    continue
                if candidate_qnos and qno not in candidate_qnos:
                    continue
                answer_text = normalize_short_answer_text(entry.get("answer_text"))
                if not answer_text:
                    continue
                try:
                    confidence = float(entry.get("confidence"))
                except (TypeError, ValueError):
                    confidence = 0.0
                entries.append(
                    {
                        "question_no": qno,
                        "answer_text": answer_text,
                        "confidence": max(0.0, min(1.0, confidence)),
                        "reason": str(entry.get("reason") or ""),
                    }
                )
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        normalized.append(
            {
                "label": label,
                "role": role,
                "candidate_qnos": candidate_qnos,
                "entries": entries,
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str(item.get("reason") or ""),
            }
        )
    risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
    return {"tables": normalized, "risks": risks}


def extract_answer_tables_with_llm(
    run_id: str,
    rows: list[dict[str, Any]],
    answer_packets: list[dict[str, Any]],
    run_dir: Path,
    model: str,
    timeout: int,
    out_path: Path,
    force: bool,
    worker_models: list[str] | None = None,
) -> dict[str, Any]:
    if out_path.exists() and not force:
        return read_json(out_path, {})
    active_models = normalize_worker_models(model, worker_models)
    contexts = answer_table_extraction_contexts(answer_packets, rows, run_dir)
    contexts, worker_partitions = assign_context_models(contexts, active_models)
    write_json(out_path.with_name(out_path.stem + "_input_compact.json"), {"run_id": run_id, "worker_partitions": worker_partitions, "pages": contexts})
    if not contexts:
        review = {
            "run_id": run_id,
            "model": multi_model_name(active_models),
            "worker_models": active_models,
            "worker_partitions": [],
            "pages": [],
            "summary": {"table_count": 0, "entry_count": 0},
        }
        write_json(out_path, review)
        return review

    def call_page(context: dict[str, Any]) -> dict[str, Any]:
        messages = build_answer_table_extraction_messages(run_id, context)
        request_model = str(context.get("_worker_model") or model)
        parsed, raw, elapsed = call_chat_json(messages, model=request_model, timeout=timeout)
        normalized = normalize_answer_table_extraction_page(parsed, context)
        return {
            "stream": context.get("stream"),
            "page": context.get("page"),
            "model": request_model,
            "llm_elapsed_seconds": round(elapsed, 3),
            "tables": normalized["tables"],
            "risks": normalized["risks"],
            "raw_response": raw,
        }

    pages: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(ANSWER_TABLE_PAGE_WORKERS, len(contexts))) as pool:
        future_map = {pool.submit(call_page, context): context for context in contexts}
        for future in as_completed(future_map):
            pages.append(future.result())
    pages.sort(key=lambda item: int(item.get("page") or 0))
    all_tables = [table for page in pages for table in page.get("tables") or []]
    all_entries = [entry for table in all_tables for entry in table.get("entries") or []]
    review = {
        "run_id": run_id,
        "model": multi_model_name(active_models),
        "worker_models": active_models,
        "worker_partitions": worker_partitions,
        "pages": pages,
        "summary": {
            "table_count": len(all_tables),
            "answer_key_table_count": sum(1 for table in all_tables if table.get("role") == "answer_key_table"),
            "entry_count": len(all_entries),
            "risk_count": sum(len(page.get("risks") or []) for page in pages),
            "elapsed_seconds_sum": round(sum(float(page.get("llm_elapsed_seconds") or 0.0) for page in pages), 3),
        },
    }
    write_json(out_path, review)
    return review


def apply_answer_table_extraction_to_rows(rows: list[dict[str, Any]], review: dict[str, Any]) -> None:
    row_by_qno = {
        int(row["question_no"]): row
        for row in rows
        if row.get("question_no") is not None
    }
    for page in review.get("pages") or []:
        for table in page.get("tables") or []:
            if table.get("role") != "answer_key_table":
                continue
            label = str(table.get("label") or "")
            for entry in table.get("entries") or []:
                try:
                    qno = int(entry.get("question_no"))
                except (TypeError, ValueError):
                    continue
                row = row_by_qno.get(qno)
                if not row:
                    continue
                answer_items = row.setdefault("answer_items", [])
                target = next((item for item in answer_items if str(item.get("role") or "answer") == "answer"), None)
                if target is None:
                    target = {
                        "question_no": qno,
                        "role": "answer",
                        "block_labels": [],
                        "span_text_excerpt": "",
                        "continues_previous": False,
                        "confidence": entry.get("confidence") or table.get("confidence") or 0.0,
                        "reason": "Step4 answer table extraction created answer item.",
                    }
                    answer_items.append(target)
                target["block_labels"] = [
                    existing
                    for existing in target.get("block_labels") or []
                    if existing != label
                ]
                spans = [span for span in target.get("text_spans") or [] if not (isinstance(span, dict) and span.get("source_label") == label)]
                answer_text = normalize_short_answer_text(entry.get("answer_text"))
                spans.append(
                    {
                        "source_label": label,
                        "role": "answer",
                        "text": answer_text,
                        "reason": entry.get("reason") or "Step4 parsed answer-key table.",
                        "confidence": entry.get("confidence"),
                    }
                )
                target["text_spans"] = spans
                target["span_text_excerpt"] = answer_text
                target["confidence"] = max(float(target.get("confidence") or 0.0), float(entry.get("confidence") or 0.0))
                row["answer_status"] = "found"


def sorted_labels_by_blocks(labels: list[str], blocks: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        unique_str(labels),
        key=lambda label: (
            int(blocks.get(label, {}).get("_packet_page") or blocks.get(label, {}).get("page") or 10**6),
            int(blocks.get(label, {}).get("_packet_order") or 10**6),
            label,
        ),
    )


def apply_visual_asset_assignment_to_rows(
    rows: list[dict[str, Any]],
    review: dict[str, Any],
    question_blocks: dict[str, dict[str, Any]],
    answer_blocks: dict[str, dict[str, Any]],
) -> None:
    row_by_qno = {
        int(row["question_no"]): row
        for row in rows
        if row.get("question_no") is not None
    }
    mode = str(review.get("mode") or "")
    question_asset_labels: set[str] = set()
    answer_asset_labels: set[str] = set()
    for page in review.get("pages") or []:
        stream = str(page.get("stream") or "")
        for asset in page.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            label = str(asset.get("label") or "")
            if not label:
                continue
            asset_part = str(asset.get("asset_part") or stream or "")
            if mode == "mixed" or stream == "mixed" or asset_part == "mixed":
                question_asset_labels.update({label, label_to_q(label)})
                answer_asset_labels.update({label, label_to_a(label)})
            elif stream == "answer" or asset_part == "answer":
                answer_asset_labels.add(label)
            else:
                question_asset_labels.add(label)
    for row in rows:
        row["visual_assets"] = []
        row["question_labels"] = [
            str(label)
            for label in row.get("question_labels") or []
            if str(label) not in question_asset_labels
        ]
        for item in row.get("answer_items") or []:
            item["block_labels"] = [
                str(label)
                for label in item.get("block_labels") or []
                if str(label) not in answer_asset_labels
            ]

    for page in review.get("pages") or []:
        for asset in page.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            role = str(asset.get("role") or "uncertain")
            if role in {"noise", "uncertain"}:
                continue
            try:
                qno = int(asset.get("question_no"))
            except (TypeError, ValueError):
                continue
            row = row_by_qno.get(qno)
            if not row:
                continue
            raw_label = str(asset.get("label") or "")
            if raw_label in question_blocks or raw_label in answer_blocks:
                label = raw_label
            elif role in ANSWER_SIDE_ASSET_ROLES:
                label = raw_label if raw_label in answer_blocks else label_to_a(raw_label)
                if label not in answer_blocks and raw_label in question_blocks:
                    label = raw_label
            else:
                label = raw_label if raw_label in question_blocks else label_to_q(raw_label)
            block = question_blocks.get(label) or answer_blocks.get(label) or question_blocks.get(raw_label) or answer_blocks.get(raw_label) or {}
            visual = {
                "label": label,
                "kind": asset.get("kind") or asset_kind(block) or "image",
                "role": role,
                "option_label": asset.get("option_label"),
                "caption_labels": [str(value) for value in asset.get("caption_labels") or []],
                "caption_text": asset.get("caption_text") or "",
                "confidence": asset.get("confidence"),
                "reason": asset.get("reason") or "",
                "candidate_qnos": asset.get("candidate_qnos") or [],
                "step3_rule_role": asset.get("step3_rule_role"),
                "rule_role_source": asset.get("rule_role_source"),
            }
            row.setdefault("visual_assets", [])
            if not any(isinstance(item, dict) and item.get("label") == label for item in row["visual_assets"]):
                row["visual_assets"].append(visual)
            if role in QUESTION_SIDE_ASSET_ROLES and label in question_blocks:
                if role in {"stem_table", "option_table"}:
                    labels = [str(item) for item in row.get("question_labels") or []]
                    labels.append(label)
                    row["question_labels"] = sorted_labels_by_blocks(labels, question_blocks)
            elif role in ANSWER_SIDE_ASSET_ROLES:
                answer_items = row.setdefault("answer_items", [])
                if not answer_items:
                    answer_items.append(
                        {
                            "question_no": qno,
                            "role": "analysis" if role != "answer_figure" else "answer",
                            "block_labels": [],
                            "span_text_excerpt": "",
                            "continues_previous": False,
                            "confidence": asset.get("confidence") or 0.0,
                            "reason": "visual asset assignment created answer-side item.",
                        }
                    )
                target_item = answer_items[0]
                if role == "rubric_table":
                    labels = [str(item) for item in target_item.get("block_labels") or []]
                    if label in answer_blocks or label in question_blocks:
                        labels.append(label)
                    target_item["block_labels"] = sorted_labels_by_blocks(labels, answer_blocks if label in answer_blocks else question_blocks)


def load_packet_dir(run_dir: Path, packet_dir_name: str) -> list[dict[str, Any]]:
    packet_dir = run_dir / packet_dir_name
    if not packet_dir.exists():
        return []
    packets = [read_json(path, {}) for path in sorted(packet_dir.glob("page_*.json"))]
    return [packet for packet in packets if isinstance(packet, dict)]


def compact_latex_items(value: Any, limit: int = 220, max_items: int = 4) -> list[str]:
    return [
        text_excerpt(text, limit=limit)
        for text in standardize.normalize_latex_blocks(value)[:max_items]
        if str(text).strip()
    ]


def compact_options_latex(options: Any) -> dict[str, list[str]]:
    if not isinstance(options, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key in ("A", "B", "C", "D"):
        items = compact_latex_items(options.get(key), limit=180, max_items=2)
        if items:
            out[key] = items
    return out


def standardized_text_for_visual_assignment(record: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "question_type": record.get("question_type") or "unknown",
        "stem": compact_latex_items(record.get("stem_latex"), limit=200, max_items=6),
        "options": compact_options_latex(record.get("options_latex")),
    }
    answer = compact_latex_items(record.get("answer_latex"), limit=160, max_items=4)
    analysis = compact_latex_items(record.get("analysis_latex"), limit=180, max_items=8)
    rubric = compact_latex_items(record.get("rubric_latex"), limit=160, max_items=4)
    if answer:
        compact["answer"] = answer
    if analysis:
        compact["analysis"] = analysis
    if rubric:
        compact["rubric"] = rubric
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def standardized_role_texts_for_visual_assignment(record: dict[str, Any]) -> dict[str, list[str]]:
    options = record.get("options_latex") if isinstance(record.get("options_latex"), dict) else {}
    option_blocks: list[str] = []
    for value in options.values():
        option_blocks.extend(standardize.normalize_latex_blocks(value))
    role_texts = {
        "stem": standardize.normalize_latex_blocks(record.get("stem_latex")) + option_blocks,
        "answer": standardize.normalize_latex_blocks(record.get("answer_latex")),
        "analysis": standardize.normalize_latex_blocks(record.get("analysis_latex")),
        "rubric": standardize.normalize_latex_blocks(record.get("rubric_latex")),
    }
    return {
        role: [text for text in values if str(text).strip()]
        for role, values in role_texts.items()
        if values
    }


def load_standardized_texts(qb_root: Path, run_id: str) -> dict[int, dict[str, Any]]:
    per_dir = qb_root / run_id / "per_question"
    out: dict[int, dict[str, Any]] = {}
    if not per_dir.exists():
        return out
    for path in sorted(per_dir.glob("q*.json")):
        if not re.fullmatch(r"q\d{2}\.json", path.name):
            continue
        record = read_json(path, {})
        if not isinstance(record, dict) or record.get("question_no") is None:
            continue
        try:
            qno = int(record["question_no"])
        except (TypeError, ValueError):
            continue
        out[qno] = {
            "compact": standardized_text_for_visual_assignment(record),
            "role_texts": standardized_role_texts_for_visual_assignment(record),
        }
    return out


def attach_standardized_texts(rows: list[dict[str, Any]], standardized: dict[int, dict[str, Any]]) -> None:
    for row in rows:
        try:
            qno = int(row.get("question_no"))
        except (TypeError, ValueError):
            continue
        entry = standardized.get(qno)
        if not entry:
            continue
        if "compact" in entry or "role_texts" in entry:
            if entry.get("compact"):
                row["standardized_text"] = entry["compact"]
            if entry.get("role_texts"):
                row["_standardized_role_texts"] = entry["role_texts"]
        else:
            row["standardized_text"] = entry


def strip_transient_row_context(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row.pop("standardized_text", None)
        row.pop("_standardized_role_texts", None)


def assign_visual_assets_for_run(
    run_id: str,
    runs_root: Path,
    qb_root: Path,
    model: str,
    timeout: int,
    force: bool,
    worker_models: list[str] | None = None,
) -> dict[str, Any]:
    run_dir = runs_root / f"{run_id}_raw_units"
    alignment_path = run_dir / "qa_alignment.json"
    alignment = read_json(alignment_path, {})
    if not alignment:
        raise FileNotFoundError(f"Missing qa_alignment.json for {run_id}: {run_dir}")
    rows = alignment.get("qa_alignment") or []
    if not isinstance(rows, list):
        rows = []

    summary_path = run_dir / "pipeline_summary.json"
    summary = read_json(summary_path, {})
    pipeline_mode = str(summary.get("mode") or "pure_paper")
    question_groups = read_json(run_dir / "question_groups.json", [])
    if not isinstance(question_groups, list):
        question_groups = []
    question_packets = load_packet_dir(run_dir, "question_packets")
    answer_packets = load_packet_dir(run_dir, "answer_packets")
    question_blocks = block_by_label(question_packets)
    answer_blocks = block_by_label(answer_packets)

    attach_standardized_texts(rows, load_standardized_texts(qb_root, run_id))
    review = assign_visual_assets_with_llm(
        run_id=run_id,
        pipeline_mode=pipeline_mode,
        question_groups=question_groups,
        rows=rows,
        question_packets=question_packets,
        answer_packets=answer_packets,
        model=model,
        timeout=timeout,
        out_path=run_dir / "visual_asset_assignment.json",
        force=force,
        worker_models=worker_models,
    )
    answer_table_review = extract_answer_tables_with_llm(
        run_id=run_id,
        rows=rows,
        answer_packets=answer_packets,
        run_dir=run_dir,
        model=model,
        timeout=timeout,
        out_path=run_dir / "answer_table_extraction.json",
        force=force,
        worker_models=worker_models,
    )
    apply_visual_asset_assignment_to_rows(rows, review, question_blocks, answer_blocks)
    apply_answer_table_extraction_to_rows(rows, answer_table_review)
    strip_transient_row_context(rows)

    qnos = {int(row["question_no"]) for row in rows if row.get("question_no") is not None}
    answer_qnos = qnos if pipeline_mode == "mixed" else {
        int(row["question_no"])
        for row in rows
        if row.get("question_no") is not None and row.get("answer_items")
    }
    visual_summary = review.get("summary") or {}
    alignment.update(
        {
            "qa_alignment": rows,
            "missing_answer_numbers": [] if pipeline_mode == "mixed" else sorted(qnos - answer_qnos),
            "visual_asset_assignment_summary": visual_summary,
            "answer_table_extraction_summary": answer_table_review.get("summary") or {},
            "asset_match_risk_count": int(visual_summary.get("risk_count") or 0),
        }
    )
    write_json(alignment_path, alignment)
    if isinstance(summary, dict):
        summary.update(
            {
                "visual_asset_assignment_timeout_seconds": timeout,
                "visual_asset_assignment_elapsed_seconds_sum": visual_summary.get("elapsed_seconds_sum"),
                "visual_asset_assignment_elapsed_seconds_max": visual_summary.get("elapsed_seconds_max"),
                "visual_asset_assignment_asset_count": visual_summary.get("asset_count"),
                "answer_table_extraction_entry_count": (answer_table_review.get("summary") or {}).get("entry_count"),
                "asset_match_risk_count": int(visual_summary.get("risk_count") or 0),
            }
        )
        write_json(summary_path, summary)
    return visual_summary


def option_asset_placeholders_from_visual_assets(assets: list[Any]) -> dict[str, dict[str, str]]:
    placeholders: dict[str, dict[str, str]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("role") or "") != "option_figure":
            continue
        option_label = str(asset.get("option_label") or "").strip().upper()
        label = str(asset.get("label") or "").strip()
        if option_label not in {"A", "B", "C", "D"} or not label:
            continue
        placeholder = str(asset.get("placeholder") or "").strip()
        placeholders[option_label] = {
            "label": label,
            **({"placeholder": placeholder} if placeholder else {}),
        }
    return placeholders


def source_answer_labels_from_row(row: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in row.get("answer_items") or []:
        if not isinstance(item, dict):
            continue
        labels.extend(str(label) for label in item.get("block_labels") or [] if str(label))
        for span in item.get("text_spans") or []:
            if isinstance(span, dict) and span.get("source_label"):
                labels.append(str(span["source_label"]))
    return unique_str(labels)


def sync_step4_assets_to_question_bank(run_id: str, runs_root: Path, qb_root: Path) -> dict[str, Any]:
    """Sync Step4 visual assignment metadata into the question-bank before render.

    JSON-schema Step3 runs before Step4, so question_bank.json needs Step4's
    final visual_assets and source labels copied in after asset assignment. This
    pass updates only asset metadata; it does not touch the model-produced
    question text, answers, or analysis.
    """
    run_dir = runs_root / f"{run_id}_raw_units"
    qb_run_dir = qb_root / run_id
    qb_path = qb_run_dir / "question_bank.json"
    alignment = read_json(run_dir / "qa_alignment.json", {})
    rows = alignment.get("qa_alignment") or [] if isinstance(alignment, dict) else []
    if not qb_path.exists() or not isinstance(rows, list):
        summary = {
            "run_id": run_id,
            "status": "skipped",
            "reason": "missing_question_bank_or_alignment",
            "changed_question_count": 0,
        }
        write_json(run_dir / "step4_question_bank_sync.json", summary)
        return summary

    payload = read_json(qb_path, [])
    records = payload.get("questions", []) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        summary = {
            "run_id": run_id,
            "status": "skipped",
            "reason": "question_bank_not_list",
            "changed_question_count": 0,
        }
        write_json(run_dir / "step4_question_bank_sync.json", summary)
        return summary

    row_by_qno = {
        int(row["question_no"]): row
        for row in rows
        if isinstance(row, dict) and row.get("question_no") is not None
    }
    changed_qnos: list[int] = []
    field_changes: dict[str, list[str]] = {}
    synced_records: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            synced_records.append(record)
            continue
        qno = int(record.get("question_no") or 0)
        row = row_by_qno.get(qno)
        if not row:
            synced_records.append(record)
            continue
        out = dict(record)
        changes: list[str] = []
        assets = [asset for asset in row.get("visual_assets") or [] if isinstance(asset, dict)]
        option_assets = option_asset_placeholders_from_visual_assets(assets)
        question_labels = unique_str([str(label) for label in row.get("question_labels") or [] if str(label)])
        answer_labels = source_answer_labels_from_row(row)
        updates = {
            "visual_assets": assets,
            "figure_labels": [],
            "option_asset_placeholders": option_assets,
            "source_question_labels": question_labels,
            "source_answer_labels": answer_labels,
        }
        for field, value in updates.items():
            if out.get(field) != value:
                out[field] = value
                changes.append(field)
        if changes:
            changed_qnos.append(qno)
            field_changes[str(qno)] = changes
        synced_records.append(out)

    if changed_qnos:
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["questions"] = synced_records
            write_json(qb_path, payload)
        else:
            write_json(qb_path, synced_records)
        write_jsonl(qb_run_dir / "question_bank.jsonl", [record for record in synced_records if isinstance(record, dict)])

    summary = {
        "run_id": run_id,
        "status": "ok",
        "changed_question_count": len(changed_qnos),
        "changed_question_numbers": changed_qnos,
        "field_changes": field_changes,
        "source": str((run_dir / "qa_alignment.json").resolve()),
        "target": str(qb_path.resolve()),
    }
    write_json(run_dir / "step4_question_bank_sync.json", summary)
    write_json(qb_run_dir / "step4_question_bank_sync.json", summary)
    return summary




def main() -> None:
    parser = argparse.ArgumentParser(description="Step4/4.5 visual asset distribution for v11 question-bank pipeline.")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--qb-root", type=Path, default=DEFAULT_QB_ROOT)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--worker-model", action="append", default=[])
    parser.add_argument("--llm-provider", choices=["siliconflow", "bailian", "env"], default=DEFAULT_LLM_PROVIDER)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--tpm-limit", type=int, default=None, help="Local LLM tokens-per-minute limit. 0 disables.")
    parser.add_argument("--token-estimator-model", default=None, help="Tokenizer/processor model. Defaults to --model.")
    parser.add_argument("--image-token-mode", choices=["auto", "processor", "formula"], default="auto")
    parser.add_argument("--tpm-output-reserve", type=int, default=DEFAULT_OUTPUT_RESERVE_TOKENS)
    parser.add_argument("--token-budget-log", type=Path, default=None)
    parser.add_argument("--token-budget-verbose", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-sync", action="store_true")
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
    summaries: list[dict[str, Any]] = []
    for run_id in args.run_id:
        visual_summary = assign_visual_assets_for_run(
            run_id=run_id,
            runs_root=args.runs_root,
            qb_root=args.qb_root,
            model=args.model,
            timeout=args.timeout,
            force=args.force,
            worker_models=args.worker_model or [args.model],
        )
        item = {"run_id": run_id, "visual_asset_assignment_summary": visual_summary}
        if not args.skip_sync:
            item["step4_question_bank_sync"] = sync_step4_assets_to_question_bank(run_id, args.runs_root, args.qb_root)
        summaries.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    write_json(args.runs_root / "step4_batch_summary.json", {"runs": summaries, "model": args.model})


if __name__ == "__main__":
    main()
