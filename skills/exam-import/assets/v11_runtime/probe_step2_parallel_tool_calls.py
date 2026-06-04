from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

WORKTREE_ROOT = Path(__file__).resolve().parent
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from common_io import data_uri, read_json, write_json  # noqa: E402
from common_llm import configure_llm_provider_env, post_chat_completion  # noqa: E402
from step2_layout import (  # noqa: E402
    LLM_IMAGE_DATA_URI_MAX_BYTES,
    build_packets,
    compact_packet,
    layout_items_for_doc,
    normalize_ranges,
)

DEFAULT_SOURCE_RUNS_ROOT = WORKTREE_ROOT / "source_runs"
DEFAULT_OUTPUT_ROOT = WORKTREE_ROOT / "tmp" / "step2_parallel_tool_probe"
TOOL_NAME = "submit_step2_question_range"

ONE_RANGE_TOOL_SCHEMA = {
    "type": "object",
    "required": ["question_no", "start_label", "end_label", "visual_labels", "confidence", "reason", "risks"],
    "properties": {
        "question_no": {"type": "integer"},
        "start_label": {"type": "string"},
        "end_label": {"type": "string"},
        "visual_labels": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "block_labels", "severity", "evidence"],
                "properties": {
                    "type": {"type": "string", "enum": ["boundary", "missing", "uncertain"]},
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


def mode_for_args(args: argparse.Namespace, source_item: dict[str, Any]) -> str:
    if args.mode != "auto":
        return str(args.mode)
    source_mode = str(source_item.get("input_mode") or source_item.get("mode") or "")
    if args.stream == "answer":
        return "pure_answer"
    if source_mode == "mixed":
        return "mixed"
    return "pure_paper"


def namespace_for(stream: str, mode: str) -> str:
    if mode == "mixed":
        return "M"
    if stream == "answer":
        return "A"
    return "Q"


def mode_rule(mode: str) -> str:
    rules = {
        "pure_paper": "当前输入是试题页。每个工具调用提交一道完整顶层题的题干、选项、图表和续写块范围。",
        "pure_answer": "当前输入是答案页或解析页。每个工具调用提交一道题完整答案、解析、解答过程或评分标准的范围。",
        "mixed": "当前输入中题干和答案解析交错出现。每个工具调用提交一道完整顶层题的题干、答案、解析、评分标准和续写块范围。",
    }
    return rules[mode]


def build_probe_messages(
    packets: list[dict[str, Any]],
    run_id: str,
    stream: str,
    mode: str,
    include_images: bool,
) -> list[dict[str, Any]]:
    compact = {
        "run_id": run_id,
        "stream": stream,
        "mode": mode,
        "pages": [compact_packet(packet) for packet in packets],
    }
    instruction = (
        "/no_think\n"
        "你是试卷 OCR 顶层题号块范围检测器。本次是 parallel tool calls 探测。\n"
        f"{mode_rule(mode)}\n\n"
        "任务：\n"
        f"- 对每个可见的顶层题号，各调用一次工具 {TOOL_NAME}。\n"
        "- 一个工具调用只能提交一个顶层题号；不得把多个题号合并到同一个工具参数中。\n"
        "- 不要在普通回复正文中输出 JSON、Markdown、代码块、解释或分析过程。\n"
        "- 工具调用数量应等于你识别出的顶层题号数量。\n"
        "- 顶层题号形如 1.、2.、21.；(1)(2) 这类小问不是新题。\n"
        "- 21-I、21-II、21-Ⅰ、21-Ⅱ 属于同一顶层题号 21，必须只调用一次工具。\n"
        "- 选择题范围必须从包含该题题号和题干的 OCR 块开始，不能从选项块开始。\n"
        "- 相邻题边界不确定时，可以让前后两题共用边界标签，但必须在 reason 中说明。\n"
        "- 如果一道题跨页续写，且后续页面没有新的顶层题号，结束标签延伸到最后一个明显属于该题的续写块。\n"
        "- 忽略页眉、页脚、大题标题、页码、二维码、水印和装饰块。\n"
        "- visual_labels 只放明显属于该题但不在 start_label 到 end_label 连续范围内的图、表或图片标签。\n\n"
        "工具参数必须是单题对象，格式如下：\n"
        + json.dumps(
            {
                "question_no": 1,
                "start_label": "Q-V01-B01",
                "end_label": "Q-V01-B03",
                "visual_labels": ["Q-V01-P01"],
                "confidence": 0.0,
                "reason": "简短中文理由",
                "risks": [
                    {
                        "type": "boundary",
                        "block_labels": ["Q-V01-B02"],
                        "severity": "warning",
                        "evidence": "简短中文证据",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n\n紧凑版面包：\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
        + f"\n\n现在并行调用工具 {TOOL_NAME}。普通正文保持为空。"
    )
    content: list[dict[str, Any]] = []
    if include_images:
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
        {
            "role": "system",
            "content": (
                f"你必须使用工具 {TOOL_NAME} 提交 Step2 单题范围。"
                "每道顶层题调用一次工具；不要在 message.content 中写结果。"
            ),
        },
        {"role": "user", "content": content},
    ]


def load_packets(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str, str, Path]:
    source_runs_root = args.source_runs_root.resolve()
    source_run_dir = source_runs_root / args.run_id
    source_item = read_json(source_run_dir / "source_item.json", {})
    mode = mode_for_args(args, source_item)
    if mode not in {"pure_paper", "pure_answer", "mixed"}:
        raise ValueError(f"Unsupported probe mode: {mode}")
    doc_part = "answer" if args.stream == "answer" else "paper"
    doc_dir = source_run_dir / doc_part
    ocr_path = doc_dir / "ocr_blocks.json"
    ocr = read_json(ocr_path, {})
    blocks = layout_items_for_doc(ocr, doc_dir)
    if not blocks:
        raise FileNotFoundError(f"missing OCR layout blocks: {ocr_path}")
    out_run_dir = args.output_root.resolve() / args.run_id / f"{args.stream}_{mode}"
    packets = build_packets(
        args.run_id,
        doc_dir,
        out_run_dir,
        blocks,
        namespace_for(args.stream, mode),
        "probe_packets",
        include_text=not args.hide_block_text,
    )
    if args.max_pages is not None:
        packets = packets[: args.max_pages]
    if not packets:
        raise ValueError("probe has no packets to send")
    return packets, mode, str(source_item.get("input_mode") or source_item.get("mode") or ""), out_run_dir


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
        parsed.append(
            {
                "index": index,
                "id": call.get("id"),
                "name": name,
                "arguments": arguments,
                "raw_arguments": raw_arguments,
            }
        )
    return parsed


def merge_probe_calls(parsed_calls: list[dict[str, Any]], packets: list[dict[str, Any]]) -> dict[str, Any]:
    ranges: list[dict[str, Any]] = []
    risks: list[dict[str, Any]] = []
    for call in parsed_calls:
        args = call["arguments"]
        ranges.append(
            {
                "question_no": args.get("question_no"),
                "start_label": args.get("start_label"),
                "end_label": args.get("end_label"),
                "visual_labels": args.get("visual_labels") or [],
                "confidence": args.get("confidence"),
                "reason": args.get("reason") or "",
            }
        )
        for risk in args.get("risks") or []:
            if isinstance(risk, dict):
                risk = dict(risk)
                risk["question_no"] = args.get("question_no")
                risks.append(risk)
    merged = {"question_ranges": ranges, "noise_blocks": [], "risks": risks}
    return normalize_ranges(merged, packets)


def build_tool_choice(kind: str) -> str | dict[str, Any]:
    if kind == "required":
        return "required"
    return {"type": "function", "function": {"name": TOOL_NAME}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Bailian parallel tool calls for Step2 one-question-per-tool range detection.")
    parser.add_argument("--source-runs-root", type=Path, default=DEFAULT_SOURCE_RUNS_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stream", choices=["paper", "answer"], default="paper")
    parser.add_argument("--mode", choices=["auto", "pure_paper", "pure_answer", "mixed"], default="auto")
    parser.add_argument("--model", default="qwen-plus")
    parser.add_argument("--provider", choices=["bailian", "bailian_batch"], default="bailian")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--tool-choice", choices=["function", "required"], default="function")
    parser.add_argument("--no-parallel-tool-calls", action="store_true")
    parser.add_argument("--include-images", action="store_true", help="Include annotated page images. Use only with a model that supports images and function calling.")
    parser.add_argument("--hide-block-text", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args()

    configure_llm_provider_env(os.environ, args.provider)
    packets, mode, source_mode, out_run_dir = load_packets(args)
    messages = build_probe_messages(
        packets,
        run_id=args.run_id,
        stream=args.stream,
        mode=mode,
        include_images=args.include_images,
    )
    compact = {
        "run_id": args.run_id,
        "stream": args.stream,
        "mode": mode,
        "source_mode": source_mode,
        "pages": [compact_packet(packet) for packet in packets],
    }
    write_json(out_run_dir / "input_compact.json", compact)
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
                    "description": "提交 Step2 中一个顶层题号的题面或答案范围。每次调用只能提交一道题。",
                    "parameters": ONE_RANGE_TOOL_SCHEMA,
                },
            }
        ],
        "tool_choice": build_tool_choice(args.tool_choice),
    }
    started = time.monotonic()
    payload = post_chat_completion(body, timeout=args.timeout)
    elapsed = time.monotonic() - started
    write_json(out_run_dir / "raw_response.json", payload)
    parsed_calls = parse_tool_calls(payload)
    write_json(out_run_dir / "parsed_tool_calls.json", parsed_calls)
    normalized = merge_probe_calls(parsed_calls, packets)
    write_json(out_run_dir / "normalized_ranges.json", normalized)
    invalid_count = sum(1 for item in normalized.get("question_ranges") or [] if not item.get("valid"))
    summary = {
        "run_id": args.run_id,
        "provider": args.provider,
        "model": args.model,
        "stream": args.stream,
        "mode": mode,
        "include_images": args.include_images,
        "parallel_tool_calls": not args.no_parallel_tool_calls,
        "tool_choice": args.tool_choice,
        "page_count": len(packets),
        "tool_call_count": len(parsed_calls),
        "normalized_question_count": len(normalized.get("question_ranges") or []),
        "invalid_range_count": invalid_count,
        "missing_question_numbers_within_detected_span": normalized.get("missing_question_numbers_within_detected_span") or [],
        "elapsed_seconds": round(elapsed, 3),
        "output_dir": str(out_run_dir),
        "raw_response": str(out_run_dir / "raw_response.json"),
        "parsed_tool_calls": str(out_run_dir / "parsed_tool_calls.json"),
        "normalized_ranges": str(out_run_dir / "normalized_ranges.json"),
    }
    write_json(out_run_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
