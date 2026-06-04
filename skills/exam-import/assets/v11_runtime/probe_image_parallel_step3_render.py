from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

WORKTREE_ROOT = Path(__file__).resolve().parent
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from common_io import data_uri, read_json, write_json, write_jsonl  # noqa: E402
from common_llm import configure_llm_provider_env, post_chat_completion  # noqa: E402
from step2_layout import (  # noqa: E402
    LLM_IMAGE_DATA_URI_MAX_BYTES,
    PageScale,
    asset_kind,
    block_order,
    build_packets,
    compact_packet,
    draw_labeled_box,
    layout_items_for_doc,
)
from step3_question2json import STEP3_IMAGE_ONLY_TOOL_SCHEMA  # noqa: E402
from step3_schema import Step3Record  # noqa: E402

DEFAULT_SOURCE_RUNS_ROOT = WORKTREE_ROOT / "source_runs"
DEFAULT_OUTPUT_ROOT = WORKTREE_ROOT / "tmp" / "image_parallel_step3_render_probe"
TOOL_NAME = "submit_image_question_record"

QUESTION_TOOL_SCHEMA = {
    "type": "object",
    "required": ["record", "source_question_labels", "source_answer_labels", "confidence", "reason"],
    "properties": {
        "record": STEP3_IMAGE_ONLY_TOOL_SCHEMA,
        "source_question_labels": {"type": "array", "items": {"type": "string"}},
        "source_answer_labels": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}


def source_run_mode(source_item: dict[str, Any]) -> str:
    return str(source_item.get("input_mode") or source_item.get("mode") or "pure_paper")


def load_doc_packets(
    run_id: str,
    source_run_dir: Path,
    part: str,
    out_run_dir: Path,
    namespace: str,
    packet_dir_name: str,
    include_text: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    doc_dir = source_run_dir / part
    ocr_path = doc_dir / "ocr_blocks.json"
    ocr = read_json(ocr_path, {})
    blocks = layout_items_for_doc(ocr, doc_dir)
    if not blocks:
        raise FileNotFoundError(f"missing OCR layout blocks: {ocr_path}")
    packets = build_packets(
        run_id,
        doc_dir,
        out_run_dir,
        blocks,
        namespace,
        packet_dir_name,
        include_text=include_text,
    )
    return packets, blocks


def asset_label_entries(packet: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for block in packet.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        label = str(block.get("label") or "")
        kind = asset_kind(block)
        if label and kind:
            entries.append({"label": label, "kind": kind})
    return entries


def compact_prompt_packet(packet: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == "asset_labels_only":
        return {
            "page": packet.get("page"),
            "asset_labels": asset_label_entries(packet),
        }
    return compact_packet(packet)


def build_prompt_compact(
    run_id: str,
    source_mode: str,
    question_packets: list[dict[str, Any]],
    answer_packets: list[dict[str, Any]],
    prompt_compact_mode: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "source_mode": source_mode,
        "prompt_compact_mode": prompt_compact_mode,
        "paper_pages": [compact_prompt_packet(packet, prompt_compact_mode) for packet in question_packets],
        "answer_pages": [compact_prompt_packet(packet, prompt_compact_mode) for packet in answer_packets],
    }


def write_asset_marked_pages(packets: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for packet in packets:
        source = Path(str(packet.get("page_image") or ""))
        if not source.exists():
            raise FileNotFoundError(f"missing page image: {source}")
        page = int(packet.get("page") or 0)
        image = Image.open(source).convert("RGB")
        scale = PageScale(width=image.width, height=image.height)
        draw = ImageDraw.Draw(image, "RGBA")
        for block in sorted((item for item in packet.get("blocks") or [] if isinstance(item, dict)), key=block_order):
            label = str(block.get("label") or "")
            kind = asset_kind(block)
            if not label or not kind or not block.get("bbox"):
                continue
            local = label.rsplit("-", 1)[-1]
            color = (214, 82, 51) if local.startswith("P") or kind in {"image", "chart"} else (42, 104, 173)
            draw_labeled_box(draw, scale, block["bbox"], label, color, width=3)
        out = out_dir / f"page_{page:03d}_asset_marked.png"
        image.save(out)
        packet["asset_marked_page_image"] = str(out)


def build_probe_messages(
    run_id: str,
    source_mode: str,
    question_packets: list[dict[str, Any]],
    answer_packets: list[dict[str, Any]],
    image_kind: str,
    prompt_compact_mode: str,
) -> list[dict[str, Any]]:
    compact = build_prompt_compact(run_id, source_mode, question_packets, answer_packets, prompt_compact_mode)
    mixed_rule = ""
    if source_mode == "mixed":
        mixed_rule = (
            "- 本次 source_mode 为 mixed，paper_pages 中可能同时包含题面、参考答案、分析、解答和点评。\n"
            "- 对 mixed 输入，answer_latex 可以填写同一题在 paper_pages 中明确可见的最终答案、故答案为、故选等内容。\n"
            "- 对 mixed 输入，analysis_latex 可以填写同一题在 paper_pages 中实际可见的【分析】、【解答】或解析步骤；不得自行推导。\n"
            "- 对 mixed 输入，source_answer_labels 填写答案或解析所在的 paper_pages 标签；没有可见答案或解析时输出空数组。\n"
        )
    image_rule = ""
    if image_kind == "asset_marked":
        image_rule = "- 输入图片是在原始页面上仅标出图片和表格资产标签；未标出 OCR 文本块。\n"
    label_rule = (
        "- source_question_labels 填写该题题面在 paper_pages 中对应的 OCR/图像标签，必须来自紧凑版面包。\n"
        "- source_answer_labels 填写该题答案或解析在 answer_pages 中对应的标签；没有答案页时输出空数组。\n"
        "- 如果无法可靠确定标签，宁可输出空数组并在 reason 中说明，不要编造标签。\n"
    )
    if prompt_compact_mode == "asset_labels_only":
        label_rule = (
            "- 本次紧凑版面包只提供图片和表格资产标签，不提供 OCR 文本块标签。\n"
            "- source_question_labels 只能填写与该题直接相关的 paper_pages 资产标签；没有相关资产时输出空数组。\n"
            "- source_answer_labels 只能填写与该题答案或解析直接相关的 answer_pages 资产标签；没有答案页或没有相关资产时输出空数组。\n"
            "- 不要因为看见题干位置而编造普通文字块标签。\n"
        )
    instruction = (
        "/no_think\n"
        "你是中文数学试题题库整理器。本次是整页图片并行 tool calls 探测。\n\n"
        "任务：\n"
        f"- 阅读所有输入图片，对每一道可见顶层试题各调用一次工具 {TOOL_NAME}。\n"
        "- 一个工具调用只能提交一道顶层题；不得把多道题合并到同一个工具参数中。\n"
        "- 不要在普通回复正文中输出 JSON、Markdown、代码块、解释或分析过程。\n"
        "- 顶层题号形如 1.、2.、21.；(1)(2) 这类小问不是新题。\n"
        "- 21-I、21-II、21-Ⅰ、21-Ⅱ 属于同一顶层题号 21，必须只调用一次工具。\n"
        "- 只根据图片中可见内容转写，不得解题，不得补充条件，不得改题意。\n"
        + mixed_rule
        + image_rule
        +
        "- 如果答案页图片存在，answer_latex 只填写答案页中明确独立给出的最终答案、答案行或各小题答案。\n"
        "- analysis_latex 只能来自答案/解析图片中实际可见的解析内容，不得自行推导。\n"
        "- 如果答案/解析图只给出最终答案、答案表或答案列表，没有可见解析步骤，则 analysis_latex 必须为空数组 []。\n"
        "- 图片中看不清、边界不确定、答案未独立给出时，必须在 record.issues 中记录。\n"
        "- record 必须满足 Step3Record；schema_version 固定为 step3_json_schema_v1。\n"
        "- options_latex 必须包含 A、B、C、D 四个键，非选择题四个键为空数组。\n"
        "- 填空位置写 <blank>，选择题作答位置写 <choice_blank>，且只能在数学环境外。\n"
        "- 数学内容统一为 LaTeX：行内 $...$，展示 $$...$$；每个数组元素必须是单行字符串。\n"
        "- 选择题答案字母必须写成完整数学块，例如 $A$、$B$。\n"
        "- 不要输出空字符串数组项，不要输出 HTML 实体。\n\n"
        "标签要求：\n"
        + label_rule
        + "\n"
        "紧凑版面包：\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
        + f"\n\n现在并行调用工具 {TOOL_NAME}。普通正文保持为空。"
    )
    content: list[dict[str, Any]] = []
    for label, packets in (("题面", question_packets), ("答案/解析", answer_packets)):
        for packet in packets:
            if image_kind in {"annotated", "both"}:
                annotated = Path(str(packet.get("annotated_page_image") or ""))
                content.append({"type": "text", "text": f"{label}第 {packet['page']} 页标注图："})
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_uri(
                                annotated,
                                max_bytes=LLM_IMAGE_DATA_URI_MAX_BYTES,
                                cache_dir=annotated.parent / "image_data_uri_cache",
                            )
                        },
                    }
                )
            if image_kind in {"raw", "both"}:
                raw = Path(str(packet.get("page_image") or ""))
                content.append({"type": "text", "text": f"{label}第 {packet['page']} 页原图："})
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_uri(
                                raw,
                                max_bytes=LLM_IMAGE_DATA_URI_MAX_BYTES,
                                cache_dir=raw.parent / "image_data_uri_cache",
                            )
                        },
                    }
                )
            if image_kind == "asset_marked":
                asset_marked = Path(str(packet.get("asset_marked_page_image") or ""))
                content.append({"type": "text", "text": f"{label}第 {packet['page']} 页资产标注图："})
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_uri(
                                asset_marked,
                                max_bytes=LLM_IMAGE_DATA_URI_MAX_BYTES,
                                cache_dir=asset_marked.parent / "image_data_uri_cache",
                            )
                        },
                    }
                )
    content.append({"type": "text", "text": instruction})
    return [
        {
            "role": "system",
            "content": (
                f"你必须使用工具 {TOOL_NAME} 提交每道题的题库记录。"
                "每道顶层题调用一次工具；不要在 message.content 中写结果。"
            ),
        },
        {"role": "user", "content": content},
    ]


def build_tool_choice(kind: str) -> str | dict[str, Any]:
    if kind == "required":
        return "required"
    return {"type": "function", "function": {"name": TOOL_NAME}}


def parse_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM API response has no message: {json.dumps(payload, ensure_ascii=False)[:1200]}") from exc
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError(f"LLM API response has no tool_calls: {json.dumps(message, ensure_ascii=False)[:1200]}")
    parsed: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        function = call.get("function") or {}
        name = function.get("name")
        if name != TOOL_NAME:
            raise RuntimeError(f"unexpected tool call {name!r} at index {index}; expected {TOOL_NAME!r}")
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, str):
            arguments = json.loads(raw_arguments)
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            raise RuntimeError(f"tool arguments must be JSON string or object at index {index}")
        if not isinstance(arguments, dict):
            raise RuntimeError(f"tool arguments must decode to object at index {index}")
        record = arguments.get("record")
        if not isinstance(record, dict):
            raise RuntimeError(f"tool call {index} has no record object")
        validated = Step3Record.model_validate(record).model_dump()
        parsed.append(
            {
                "index": index,
                "id": call.get("id"),
                "name": name,
                "record": validated,
                "source_question_labels": [str(label) for label in arguments.get("source_question_labels") or []],
                "source_answer_labels": [str(label) for label in arguments.get("source_answer_labels") or []],
                "confidence": arguments.get("confidence"),
                "reason": arguments.get("reason") or "",
                "raw_arguments": raw_arguments,
            }
        )
    return parsed


def unique_sorted_records(parsed_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_qno: dict[int, dict[str, Any]] = {}
    for call in parsed_calls:
        record = call["record"]
        qno = int(record.get("question_no") or 0)
        if qno <= 0:
            raise ValueError(f"invalid question_no in tool call {call.get('index')}: {qno}")
        if qno in by_qno:
            raise ValueError(f"duplicate question_no from tool calls: {qno}")
        by_qno[qno] = record
    return [by_qno[qno] for qno in sorted(by_qno)]


def labels_by_qno(parsed_calls: list[dict[str, Any]], key: str) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for call in parsed_calls:
        qno = int(call["record"].get("question_no") or 0)
        labels: list[str] = []
        for label in call.get(key) or []:
            if label and label not in labels:
                labels.append(label)
        out[qno] = labels
    return out


def all_packet_labels(packets: list[dict[str, Any]]) -> set[str]:
    return {
        str(block.get("label") or "")
        for packet in packets
        for block in packet.get("blocks") or []
        if str(block.get("label") or "")
    }


def filter_known_labels(labels: list[str], known: set[str]) -> list[str]:
    return [label for label in labels if label in known]


def write_question_bank(qb_root: Path, run_id: str, records: list[dict[str, Any]], model: str) -> dict[str, Any]:
    run_dir = qb_root / run_id
    per_dir = run_dir / "per_question"
    per_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        qno = int(record["question_no"])
        write_json(per_dir / f"q{qno:02d}.json", record)
    write_json(run_dir / "question_bank.json", records)
    write_jsonl(run_dir / "question_bank.jsonl", records)
    summary = {
        "run_id": run_id,
        "model": model,
        "standardizer": "image_parallel_tool_probe",
        "question_count": len(records),
        "success_count": sum(
            1
            for record in records
            if not any(issue.get("severity") == "error" for issue in record.get("issues") or [] if isinstance(issue, dict))
        ),
        "error_count": sum(
            1
            for record in records
            if any(issue.get("severity") == "error" for issue in record.get("issues") or [] if isinstance(issue, dict))
        ),
    }
    write_json(run_dir / "summary.json", summary)
    return summary


def packet_blocks_by_label(packets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(block.get("label") or ""): block
        for packet in packets
        for block in packet.get("blocks") or []
        if str(block.get("label") or "")
    }


def write_probe_step2_artifacts(
    block_root: Path,
    run_id: str,
    records: list[dict[str, Any]],
    parsed_calls: list[dict[str, Any]],
    question_packets: list[dict[str, Any]],
    answer_packets: list[dict[str, Any]],
    pipeline_mode: str,
) -> dict[str, Any]:
    run_dir = block_root / f"{run_id}_raw_units"
    q_labels_raw = labels_by_qno(parsed_calls, "source_question_labels")
    a_labels_raw = labels_by_qno(parsed_calls, "source_answer_labels")
    known_question = all_packet_labels(question_packets)
    known_answer = all_packet_labels(answer_packets)
    if pipeline_mode == "mixed" and not known_answer:
        known_answer = known_question
    q_labels = {qno: filter_known_labels(labels, known_question) for qno, labels in q_labels_raw.items()}
    a_labels = {qno: filter_known_labels(labels, known_answer) for qno, labels in a_labels_raw.items()}
    question_blocks = packet_blocks_by_label(question_packets)
    question_groups: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for record in records:
        qno = int(record["question_no"])
        question_labels = q_labels.get(qno) or []
        answer_labels = a_labels.get(qno) or []
        visual_assets = [
            {"label": label, "kind": asset_kind(question_blocks.get(label, {})) or "image", "role": "uncertain"}
            for label in question_labels
            if asset_kind(question_blocks.get(label, {}))
        ]
        question_groups.append(
            {
                "question_no": qno,
                "block_labels": question_labels,
                "visual_assets": visual_assets,
                "source": "image_parallel_tool_probe",
            }
        )
        rows.append(
            {
                "question_no": qno,
                "question_status": "found" if question_labels else "label_missing",
                "answer_status": "found" if answer_labels else "missing",
                "question_labels": question_labels,
                "question_core_labels": question_labels,
                "question_surface_labels": question_labels,
                "visual_assets": [],
                "answer_items": [
                    {
                        "question_no": qno,
                        "role": "answer",
                        "block_labels": answer_labels,
                        "text_spans": [{"source_label": label} for label in answer_labels],
                        "span_text_excerpt": "",
                        "continues_previous": False,
                        "confidence": None,
                        "reason": "image parallel tool probe source_answer_labels",
                    }
                ]
                if answer_labels
                else [],
            }
        )
    pipeline_summary = {
        "run_id": run_id,
        "mode": pipeline_mode,
        "question_count": len(records),
        "answered_question_count": sum(1 for row in rows if row.get("answer_items")),
        "missing_answer_numbers": [int(row["question_no"]) for row in rows if not row.get("answer_items")],
        "source": "image_parallel_tool_probe",
    }
    write_json(run_dir / "question_groups.json", question_groups)
    write_json(run_dir / "qa_alignment.json", {"qa_alignment": rows})
    write_json(run_dir / "pipeline_summary.json", pipeline_summary)
    write_json(run_dir / "step2_image_parallel_probe_label_summary.json", {"question_labels": q_labels, "answer_labels": a_labels})
    return pipeline_summary


def run_command(cmd: list[str], env: dict[str, str], cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    result = {
        "cmd": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(cmd)}\n{completed.stderr[-1200:]}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe image-only parallel Step3 tool calls and render through Step3.5/Step4/Step5.")
    parser.add_argument("--source-runs-root", type=Path, default=DEFAULT_SOURCE_RUNS_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="qwen3-vl-plus")
    parser.add_argument("--text-model", default="qwen-plus")
    parser.add_argument("--asset-model", default="qwen3-vl-plus")
    parser.add_argument("--provider", choices=["bailian", "bailian_batch"], default="bailian")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--tool-choice", choices=["function", "required"], default="function")
    parser.add_argument("--no-parallel-tool-calls", action="store_true")
    parser.add_argument(
        "--reuse-parsed-tool-calls",
        action="store_true",
        help="Skip the Step3 model call and reuse the existing parsed_tool_calls.json under the probe output dir.",
    )
    parser.add_argument("--image-kind", choices=["annotated", "raw", "both", "asset_marked"], default="annotated")
    parser.add_argument(
        "--prompt-compact-mode",
        choices=["full", "asset_labels_only"],
        default="full",
        help="Control the compact layout JSON embedded in the Step3 prompt.",
    )
    parser.add_argument("--max-paper-pages", type=int, default=None)
    parser.add_argument("--max-answer-pages", type=int, default=None)
    parser.add_argument("--no-answer-images", action="store_true")
    parser.add_argument("--hide-block-text", action="store_true")
    parser.add_argument("--step3-5-mode", choices=["full", "audit"], default="full")
    parser.add_argument("--skip-step3-5", action="store_true")
    parser.add_argument("--skip-step4", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args()

    configure_llm_provider_env(os.environ, args.provider)
    env = dict(os.environ)
    source_runs_root = args.source_runs_root.resolve()
    source_run_dir = source_runs_root / args.run_id
    source_item = read_json(source_run_dir / "source_item.json", {})
    source_mode = source_run_mode(source_item)
    base_dir = args.output_root.resolve() / args.run_id / "image_parallel_step3"
    raw_response_path = base_dir / "raw_response.json"
    parsed_tool_calls_path = base_dir / "parsed_tool_calls.json"
    block_root = base_dir / "step2_exam_blocks"
    qb_root = base_dir / "question_bank"
    step35_root = base_dir / (
        "reviews_step3_5_full_latex_normalize"
        if args.step3_5_mode == "full"
        else "reviews_step3_5_latex_audit"
    )
    render_root = base_dir / "rendered_question_bank_mathjax"
    run_block_dir = block_root / f"{args.run_id}_raw_units"
    question_packets, question_blocks = load_doc_packets(
        args.run_id,
        source_run_dir,
        "paper",
        run_block_dir,
        "M" if source_mode == "mixed" else "Q",
        "question_packets",
        include_text=not args.hide_block_text,
    )
    if args.max_paper_pages is not None:
        question_packets = question_packets[: args.max_paper_pages]
    answer_packets: list[dict[str, Any]] = []
    answer_blocks: list[dict[str, Any]] = []
    if not args.no_answer_images and (source_run_dir / "answer" / "ocr_blocks.json").exists():
        answer_packets, answer_blocks = load_doc_packets(
            args.run_id,
            source_run_dir,
            "answer",
            run_block_dir,
            "A",
            "answer_packets",
            include_text=not args.hide_block_text,
        )
        if args.max_answer_pages is not None:
            answer_packets = answer_packets[: args.max_answer_pages]
    else:
        write_json(run_block_dir / "answer_layout_items.json", [])
        (run_block_dir / "answer_packets").mkdir(parents=True, exist_ok=True)
    write_json(run_block_dir / "question_layout_items.json", question_blocks)
    write_json(run_block_dir / "answer_layout_items.json", answer_blocks)
    if args.image_kind == "asset_marked":
        write_asset_marked_pages(question_packets, run_block_dir / "asset_marked_pages" / "paper")
        if answer_packets:
            write_asset_marked_pages(answer_packets, run_block_dir / "asset_marked_pages" / "answer")

    messages = build_probe_messages(
        args.run_id,
        source_mode=source_mode,
        question_packets=question_packets,
        answer_packets=answer_packets,
        image_kind=args.image_kind,
        prompt_compact_mode=args.prompt_compact_mode,
    )
    compact = build_prompt_compact(args.run_id, source_mode, question_packets, answer_packets, args.prompt_compact_mode)
    write_json(base_dir / "input_compact.json", compact)
    body = {
        "model": args.model,
        "messages": messages,
        "temperature": 0.0,
        "top_p": 0.8,
        "enable_thinking": args.enable_thinking,
        "parallel_tool_calls": not args.no_parallel_tool_calls,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "description": "提交从整页图片整理出的一道数学试题题库记录及其源标签。",
                    "parameters": QUESTION_TOOL_SCHEMA,
                },
            }
        ],
        "tool_choice": build_tool_choice(args.tool_choice),
    }
    reused_step3 = False
    if args.reuse_parsed_tool_calls:
        raw_parsed_calls = read_json(parsed_tool_calls_path, [])
        if not isinstance(raw_parsed_calls, list):
            raise RuntimeError(f"parsed tool calls must be a list: {parsed_tool_calls_path}")
        parsed_calls = []
        for index, call in enumerate(raw_parsed_calls):
            if not isinstance(call, dict):
                raise RuntimeError(f"parsed tool call must be an object at index {index}: {parsed_tool_calls_path}")
            record = call.get("record")
            if not isinstance(record, dict):
                raise RuntimeError(f"parsed tool call has no record object at index {index}: {parsed_tool_calls_path}")
            normalized_call = dict(call)
            normalized_call["record"] = Step3Record.model_validate(record).model_dump()
            parsed_calls.append(normalized_call)
        elapsed = 0.0
        reused_step3 = True
    else:
        started = time.monotonic()
        payload = post_chat_completion(body, timeout=args.timeout)
        elapsed = time.monotonic() - started
        write_json(raw_response_path, payload)
        parsed_calls = parse_tool_calls(payload)
        write_json(parsed_tool_calls_path, parsed_calls)
    records = unique_sorted_records(parsed_calls)
    qb_summary = write_question_bank(qb_root, args.run_id, records, args.model)
    pipeline_mode = "paper_plus_answer_file" if answer_packets else ("mixed" if source_mode == "mixed" else "pure_paper")
    pipeline_summary = write_probe_step2_artifacts(
        block_root,
        args.run_id,
        records,
        parsed_calls,
        question_packets,
        answer_packets,
        pipeline_mode,
    )

    command_results: list[dict[str, Any]] = []
    if not args.skip_step3_5:
        step35_script = (
            "step3_5_full_latex_normalize.py"
            if args.step3_5_mode == "full"
            else "step3_5_question_json_audit_fix.py"
        )
        command_results.append(
            run_command(
                [
                    sys.executable,
                    str(WORKTREE_ROOT / step35_script),
                    "--run-id",
                    args.run_id,
                    "--qb-root",
                    str(qb_root),
                    "--output-root",
                    str(step35_root),
                    "--model",
                    args.text_model,
                    "--timeout",
                    str(args.timeout),
                    "--structured-output",
                    "tool_calling",
                    "--source-stage",
                    "question_bank",
                    "--write-back",
                ],
                env=env,
                cwd=WORKTREE_ROOT,
            )
        )
    if not args.skip_step4:
        command_results.append(
            run_command(
                [
                    sys.executable,
                    str(WORKTREE_ROOT / "step4_asset_distribution.py"),
                    "--runs-root",
                    str(block_root),
                    "--qb-root",
                    str(qb_root),
                    "--run-id",
                    args.run_id,
                    "--model",
                    args.asset_model,
                    "--llm-provider",
                    args.provider,
                    "--timeout",
                    str(args.timeout),
                    "--force",
                    "--structured-output",
                    "tool_calling",
                ],
                env=env,
                cwd=WORKTREE_ROOT,
            )
        )
    render_entry = ""
    if not args.skip_render:
        result = run_command(
            [
                sys.executable,
                str(WORKTREE_ROOT / "step5_vlm_html_mathjax_render.py"),
                "--run-id",
                args.run_id,
                "--qb-root",
                str(qb_root),
                "--asset-root",
                str(render_root),
                "--block-root",
                str(block_root),
                "--source-runs",
                str(source_runs_root),
                "--output-root",
                str(render_root),
            ],
            env=env,
            cwd=WORKTREE_ROOT,
        )
        command_results.append(result)
        try:
            render_payload = json.loads(result["stdout"])
            runs = render_payload.get("runs") or []
            render_entry = str(runs[0]) if runs else str(render_payload.get("index") or "")
        except json.JSONDecodeError:
            render_entry = str(render_root / args.run_id / "index.html")

    summary = {
        "run_id": args.run_id,
        "provider": args.provider,
        "model": args.model,
        "text_model": args.text_model,
        "asset_model": args.asset_model,
        "image_kind": args.image_kind,
        "prompt_compact_mode": args.prompt_compact_mode,
        "paper_page_count": len(question_packets),
        "answer_page_count": len(answer_packets),
        "parallel_tool_calls": not args.no_parallel_tool_calls,
        "reused_parsed_tool_calls": reused_step3,
        "step3_5_mode": args.step3_5_mode,
        "tool_call_count": len(parsed_calls),
        "question_count": len(records),
        "qb_success_count": qb_summary.get("success_count"),
        "qb_error_count": qb_summary.get("error_count"),
        "pipeline_mode": pipeline_mode,
        "missing_answer_numbers": pipeline_summary.get("missing_answer_numbers") or [],
        "elapsed_step3_seconds": round(elapsed, 3),
        "base_dir": str(base_dir),
        "question_bank": str(qb_root / args.run_id / "question_bank.json"),
        "step35_summary": str(step35_root / args.run_id / "summary.json"),
        "step4_summary": str(block_root / "step4_batch_summary.json"),
        "render_entry": render_entry,
        "raw_response": None if reused_step3 else str(raw_response_path),
        "parsed_tool_calls": str(parsed_tool_calls_path),
        "command_results": command_results,
    }
    write_json(base_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
