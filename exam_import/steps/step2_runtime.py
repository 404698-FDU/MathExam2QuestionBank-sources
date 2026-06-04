from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from exam_import.core.io import data_uri, read_json, write_json
from exam_import.core.run_context import RunContext
from exam_import.llm import load_and_resolve_call_spec, parse_tool_call_arguments, resolve_tool_schema
from exam_import.llm.client import LLMClient, LLMResponse
from exam_import.llm.request_builder import build_request_payload
from exam_import.prompts.loader import PromptLoader
from exam_import.schemas.common import ValidationError
from exam_import.schemas.pipeline_summary import PipelineSummary
from exam_import.schemas.qa_alignment import QAAlignmentDocument, load_qa_alignment
from exam_import.schemas.question_ranges import QuestionRangesResult
from exam_import.sources.ocr_blocks import build_packets, layout_items_for_part, load_packets
from .step2_crop import PacketGeometry, build_crop_plan
from .step2_layout import build_pipeline_summary, build_qa_alignment_document


MODE_RULES = {
    "pure_paper": "pure_paper：当前输入是试题页。每个范围是一道完整顶层题，包含题干、选项、图表和续写块。",
    "pure_answer": "pure_answer：当前输入是答案页或解析页。每个范围是一道题完整的答案、解析、解答过程或评分标准。",
    "mixed": "mixed：当前输入中题干和答案解析交错出现。每个范围是一道完整顶层题，包含题干、选项、图表、答案、解析、评分标准和续写块。",
}


@dataclass(frozen=True)
class Step2RunResult:
    qa_alignment: QAAlignmentDocument
    pipeline_summary: PipelineSummary
    question_range_count: int
    answer_range_count: int
    question_crop_count: int
    answer_crop_count: int

    def to_metrics(self) -> dict[str, Any]:
        return {
            "question_count": self.pipeline_summary.question_count,
            "answered_question_count": self.pipeline_summary.answered_question_count,
            "missing_answer_numbers": self.pipeline_summary.missing_answer_numbers,
            "extra_answer_numbers": self.pipeline_summary.extra_answer_numbers,
            "question_range_count": self.question_range_count,
            "answer_range_count": self.answer_range_count,
            "question_crop_count": self.question_crop_count,
            "answer_crop_count": self.answer_crop_count,
        }


def run_step2(
    *,
    run_context: RunContext,
    call_spec_path: Path,
    force: bool = False,
    prompt_loader: PromptLoader | None = None,
    client: LLMClient | None = None,
    env: dict[str, str] | None = None,
) -> Step2RunResult:
    prompt_loader = prompt_loader or PromptLoader()
    client = client or LLMClient()
    qa_path = run_context.step2_run_dir / "qa_alignment.json"
    summary_path = run_context.step2_run_dir / "pipeline_summary.json"
    if qa_path.exists() and summary_path.exists() and not force and run_context.alignment_mode != "answer_patch":
        qa_alignment = load_qa_alignment(qa_path)
        pipeline_summary = PipelineSummary.from_dict(read_json(summary_path))
        crop_manifest = read_json(run_context.step2_run_dir / "crops_manifest.json", default={})
        return Step2RunResult(
            qa_alignment=qa_alignment,
            pipeline_summary=pipeline_summary,
            question_range_count=len(qa_alignment.qa_alignment),
            answer_range_count=sum(len(row.answer.items) for row in qa_alignment.qa_alignment),
            question_crop_count=int(crop_manifest.get("question_crop_count") or 0),
            answer_crop_count=int(crop_manifest.get("answer_crop_count") or 0),
        )

    run_context.step2_run_dir.mkdir(parents=True, exist_ok=True)
    question_packets, answer_packets, question_ranges_result, answer_ranges_result, existing_alignment = _prepare_step2_packets_and_ranges(
        run_context=run_context,
        call_spec_path=call_spec_path,
        prompt_loader=prompt_loader,
        client=client,
        env=env,
    )

    qa_alignment = build_qa_alignment_document(
        run_id=run_context.run_id,
        alignment_mode=run_context.alignment_mode,
        question_ranges=[item.to_dict() for item in question_ranges_result.question_ranges],
        question_packets=_flatten_packet_blocks(question_packets),
        answer_ranges=[item.to_dict() for item in answer_ranges_result.question_ranges] if answer_ranges_result else None,
        answer_packets=_flatten_packet_blocks(answer_packets) if answer_packets else None,
        existing_alignment=existing_alignment,
    )
    pipeline_summary = build_pipeline_summary(qa_alignment)
    write_json(qa_path, qa_alignment.to_dict())
    write_json(summary_path, pipeline_summary.to_dict())

    crop_manifest = write_step2_crops(
        run_context=run_context,
        qa_alignment=qa_alignment,
        question_packets=question_packets,
        answer_packets=answer_packets,
        rewrite_question_crops=run_context.alignment_mode != "answer_patch",
    )
    return Step2RunResult(
        qa_alignment=qa_alignment,
        pipeline_summary=pipeline_summary,
        question_range_count=len(question_ranges_result.question_ranges),
        answer_range_count=len(answer_ranges_result.question_ranges) if answer_ranges_result else 0,
        question_crop_count=int(crop_manifest.get("question_crop_count") or 0),
        answer_crop_count=int(crop_manifest.get("answer_crop_count") or 0),
    )


def build_step2_messages(
    *,
    run_id: str,
    packets: list[dict[str, Any]],
    range_mode: str,
    tool_name: str,
    prompt_loader: PromptLoader,
    prompt_ref: str = "step2_layout",
) -> list[dict[str, Any]]:
    if range_mode not in MODE_RULES:
        raise ValueError(f"Unsupported range_mode: {range_mode}")
    compact = _compact_step2_payload(run_id=run_id, range_mode=range_mode, packets=packets)
    system_prompt = prompt_loader.load_system_text(prompt_ref).replace(
        "submit_question_ranges",
        tool_name,
    )
    user_prompt = prompt_loader.render_user_text(prompt_ref)
    user_prompt = (
        user_prompt
        .replace("{mode_rule}", MODE_RULES[range_mode])
        .replace("{output_rule}", _output_rule(tool_name))
        .replace("{compact_json}", json.dumps(compact, ensure_ascii=False, indent=2))
        .replace("{final_rule}", f"必须调用工具 {tool_name}，不要在正文中输出任何内容。")
    )
    content: list[dict[str, Any]] = []
    for packet in packets:
        content.append({"type": "text", "text": f"第 {packet['page']} 页标注图："})
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": data_uri(Path(str(packet["annotated_page_image"])))},
            }
        )
    content.append({"type": "text", "text": user_prompt})
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def call_step2_range_detection(
    *,
    run_id: str,
    packets: list[dict[str, Any]],
    range_mode: str,
    call_spec_path: Path,
    client: LLMClient | None = None,
    prompt_loader: PromptLoader | None = None,
    env: dict[str, str] | None = None,
) -> QuestionRangesResult:
    prompt_loader = prompt_loader or PromptLoader()
    resolved = load_and_resolve_call_spec(call_spec_path, prompt_loader=prompt_loader)
    tool_name = resolved.call_spec.tool_name or "submit_question_ranges"
    messages = build_step2_messages(
        run_id=run_id,
        packets=packets,
        range_mode=range_mode,
        tool_name=tool_name,
        prompt_loader=prompt_loader,
        prompt_ref=resolved.call_spec.prompt,
    )
    tool_schema = resolve_tool_schema(resolved.call_spec.tool_schema or "Step2QuestionRanges")
    payload = build_request_payload(
        resolved,
        messages=messages,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": "提交试卷 OCR 顶层题号块范围检测结果。",
                    "parameters": tool_schema,
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": tool_name}},
    )
    client = client or LLMClient()
    response = client.send_chat(resolved, payload, env=env)
    return parse_step2_response(response, expected_tool_name=tool_name, packets=packets)


def parse_step2_response(
    response: LLMResponse,
    *,
    expected_tool_name: str,
    packets: list[dict[str, Any]],
) -> QuestionRangesResult:
    payload = parse_tool_call_arguments(response, expected_tool_name=expected_tool_name)
    result = QuestionRangesResult.from_dict(payload)
    _validate_ranges_against_packets(result, packets)
    return result


def write_step2_crops(
    *,
    run_context: RunContext,
    qa_alignment: QAAlignmentDocument,
    question_packets: list[dict[str, Any]],
    answer_packets: list[dict[str, Any]],
    rewrite_question_crops: bool,
) -> dict[str, Any]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to render Step2 crop images") from exc

    crop_dir = run_context.step2_run_dir / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    question_geometries, question_widths = _packet_geometries(question_packets, stream="question")
    answer_geometries, answer_widths = _packet_geometries(answer_packets, stream="answer")
    dim_cache: dict[str, tuple[int, int]] = {}
    question_crops: dict[int, list[str]] = {}
    answer_crops: dict[int, list[str]] = {}

    if rewrite_question_crops:
        for path in crop_dir.glob(f"{run_context.run_id}_q*_question_surface_*.png"):
            path.unlink()
    for path in crop_dir.glob(f"{run_context.run_id}_q*_answer_surface_*.png"):
        path.unlink()

    for row in qa_alignment.qa_alignment:
        if rewrite_question_crops:
            question_labels = list(dict.fromkeys(row.question.labels))
            if not question_labels:
                raise ValidationError(f"Step2 question labels are empty for q{row.question_no}")
            plans = build_crop_plan(question_labels, question_geometries, question_widths)
            if not plans:
                raise ValidationError(f"Step2 crop plan is empty for q{row.question_no} question side")
            question_crops[row.question_no] = [
                str(
                    _write_crop_image(
                        crop_dir=crop_dir,
                        run_id=run_context.run_id,
                        question_no=row.question_no,
                        role="question",
                        index=index,
                        island=island,
                        image_cache=dim_cache,
                    )
                )
                for index, island in enumerate(plans, start=1)
            ]

        answer_labels = _unique_answer_labels(row)
        if answer_labels:
            plans = build_crop_plan(answer_labels, answer_geometries, answer_widths)
            if not plans:
                raise ValidationError(f"Step2 crop plan is empty for q{row.question_no} answer side")
            answer_crops[row.question_no] = [
                str(
                    _write_crop_image(
                        crop_dir=crop_dir,
                        run_id=run_context.run_id,
                        question_no=row.question_no,
                        role="answer",
                        index=index,
                        island=island,
                        image_cache=dim_cache,
                    )
                )
                for index, island in enumerate(plans, start=1)
            ]

    manifest = {
        "run_id": run_context.run_id,
        "question_crop_count": sum(len(values) for values in question_crops.values()),
        "answer_crop_count": sum(len(values) for values in answer_crops.values()),
        "question_crops": {str(key): value for key, value in question_crops.items()},
        "answer_crops": {str(key): value for key, value in answer_crops.items()},
    }
    write_json(run_context.step2_run_dir / "crops_manifest.json", manifest)
    return manifest


def _prepare_step2_packets_and_ranges(
    *,
    run_context: RunContext,
    call_spec_path: Path,
    prompt_loader: PromptLoader,
    client: LLMClient,
    env: dict[str, str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], QuestionRangesResult, QuestionRangesResult | None, QAAlignmentDocument | None]:
    source_run_dir = run_context.source_run_dir
    question_packets: list[dict[str, Any]] = []
    answer_packets: list[dict[str, Any]] = []
    question_ranges_result: QuestionRangesResult
    answer_ranges_result: QuestionRangesResult | None = None
    existing_alignment: QAAlignmentDocument | None = None

    if run_context.alignment_mode in {"pure_paper", "paper_plus_answer", "answer_patch"}:
        paper_dir = source_run_dir / "paper"
        if run_context.alignment_mode == "answer_patch":
            existing_alignment = _load_existing_alignment_for_answer_patch(run_context)
            question_packets = _prepare_or_load_question_packets(run_context=run_context, paper_dir=paper_dir)
            question_ranges_result = QuestionRangesResult(
                question_ranges=[],
                noise_blocks=[],
                risks=[],
            )
        else:
            question_packets = _prepare_question_packets(run_context=run_context, part_dir=paper_dir)
            question_ranges_result = call_step2_range_detection(
                run_id=run_context.run_id,
                packets=question_packets,
                range_mode="pure_paper",
                call_spec_path=call_spec_path,
                client=client,
                prompt_loader=prompt_loader,
                env=env,
            )
            write_json(
                run_context.step2_run_dir / "step2_pure_paper_ranges_input_compact.json",
                _compact_step2_payload(run_id=run_context.run_id, range_mode="pure_paper", packets=question_packets),
            )
            write_json(
                run_context.step2_run_dir / "step2_pure_paper_ranges.json",
                question_ranges_result.to_dict(),
            )
        if run_context.alignment_mode in {"paper_plus_answer", "answer_patch"}:
            answer_dir = source_run_dir / "answer"
            answer_packets = _prepare_answer_packets(run_context=run_context, part_dir=answer_dir)
            answer_ranges_result = call_step2_range_detection(
                run_id=run_context.run_id,
                packets=answer_packets,
                range_mode="pure_answer",
                call_spec_path=call_spec_path,
                client=client,
                prompt_loader=prompt_loader,
                env=env,
            )
            write_json(
                run_context.step2_run_dir / "step2_pure_answer_ranges_input_compact.json",
                _compact_step2_payload(run_id=run_context.run_id, range_mode="pure_answer", packets=answer_packets),
            )
            write_json(
                run_context.step2_run_dir / "step2_pure_answer_ranges.json",
                answer_ranges_result.to_dict(),
            )
    elif run_context.alignment_mode == "mixed":
        mixed_dir = source_run_dir / "mixed"
        question_packets = _prepare_mixed_packets(run_context=run_context, part_dir=mixed_dir)
        question_ranges_result = call_step2_range_detection(
            run_id=run_context.run_id,
            packets=question_packets,
            range_mode="mixed",
            call_spec_path=call_spec_path,
            client=client,
            prompt_loader=prompt_loader,
            env=env,
        )
        write_json(
            run_context.step2_run_dir / "step2_mixed_ranges_input_compact.json",
            _compact_step2_payload(run_id=run_context.run_id, range_mode="mixed", packets=question_packets),
        )
        write_json(
            run_context.step2_run_dir / "step2_mixed_ranges.json",
            question_ranges_result.to_dict(),
        )
    else:
        raise ValueError(f"Unsupported alignment_mode: {run_context.alignment_mode}")

    return question_packets, answer_packets, question_ranges_result, answer_ranges_result, existing_alignment


def _prepare_question_packets(*, run_context: RunContext, part_dir: Path) -> list[dict[str, Any]]:
    blocks = layout_items_for_part(part_dir)
    if not blocks:
        raise FileNotFoundError(f"Missing question-side OCR layout items: {part_dir / 'ocr_blocks.json'}")
    write_json(run_context.step2_run_dir / "question_layout_items.json", blocks)
    return build_packets(
        run_id=run_context.run_id,
        part_dir=part_dir,
        out_run_dir=run_context.step2_run_dir,
        blocks=blocks,
        namespace="Q",
        packet_dir_name="question_packets",
    )


def _prepare_or_load_question_packets(*, run_context: RunContext, paper_dir: Path) -> list[dict[str, Any]]:
    packet_dir = run_context.step2_run_dir / "question_packets"
    if packet_dir.exists():
        packets = load_packets(packet_dir)
        if packets:
            return packets
    return _prepare_question_packets(run_context=run_context, part_dir=paper_dir)


def _prepare_answer_packets(*, run_context: RunContext, part_dir: Path) -> list[dict[str, Any]]:
    blocks = layout_items_for_part(part_dir)
    if not blocks:
        raise FileNotFoundError(f"Missing answer-side OCR layout items: {part_dir / 'ocr_blocks.json'}")
    write_json(run_context.step2_run_dir / "answer_layout_items.json", blocks)
    return build_packets(
        run_id=run_context.run_id,
        part_dir=part_dir,
        out_run_dir=run_context.step2_run_dir,
        blocks=blocks,
        namespace="A",
        packet_dir_name="answer_packets",
    )


def _prepare_mixed_packets(*, run_context: RunContext, part_dir: Path) -> list[dict[str, Any]]:
    blocks = layout_items_for_part(part_dir)
    if not blocks:
        raise FileNotFoundError(f"Missing mixed OCR layout items: {part_dir / 'ocr_blocks.json'}")
    write_json(run_context.step2_run_dir / "question_layout_items.json", blocks)
    return build_packets(
        run_id=run_context.run_id,
        part_dir=part_dir,
        out_run_dir=run_context.step2_run_dir,
        blocks=blocks,
        namespace="Q",
        packet_dir_name="question_packets",
    )


def _load_existing_alignment_for_answer_patch(run_context: RunContext) -> QAAlignmentDocument:
    qa_path = run_context.step2_run_dir / "qa_alignment.json"
    if not qa_path.exists():
        raise FileNotFoundError(f"answer_patch requires existing Step2 qa_alignment.json: {qa_path}")
    return load_qa_alignment(qa_path)


def _compact_step2_payload(*, run_id: str, range_mode: str, packets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "range_mode": range_mode,
        "pages": [
            {
                "page": packet.get("page"),
                "annotated_page_image": packet.get("annotated_page_image"),
                "blocks": [
                    {
                        "label": block.get("label"),
                        "kind": block.get("kind") or block.get("type") or "text",
                        "text": block.get("text") or "",
                        "bbox": block.get("bbox"),
                    }
                    for block in packet.get("blocks") or []
                    if block.get("label")
                ],
            }
            for packet in packets
        ],
    }


def _output_rule(tool_name: str) -> str:
    return (
        f"输出时必须调用工具 {tool_name}。\n"
        "- tool 参数根字段必须包含 question_ranges、noise_blocks、risks。\n"
        "- question_ranges 不得为空。\n"
        "- noise_blocks 和 risks 没有内容时必须返回空数组。"
    )


def _validate_ranges_against_packets(result: QuestionRangesResult, packets: list[dict[str, Any]]) -> None:
    labels = {
        str(block.get("label"))
        for packet in packets
        for block in packet.get("blocks") or []
        if block.get("label")
    }
    seen_qnos: set[int] = set()
    for item in result.question_ranges:
        if item.question_no in seen_qnos:
            raise ValidationError(f"Duplicate question_no in Step2 result: {item.question_no}")
        seen_qnos.add(item.question_no)
        if item.start_label not in labels or item.end_label not in labels:
            raise ValidationError(
                f"Step2 returned unknown labels for q{item.question_no}: {item.start_label} -> {item.end_label}"
            )
        for label in item.visual_labels:
            if label not in labels:
                raise ValidationError(f"Step2 returned unknown visual label for q{item.question_no}: {label}")


def _flatten_packet_blocks(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for packet in packets:
        page = int(packet.get("page") or 0)
        for block in packet.get("blocks") or []:
            row = dict(block)
            row.setdefault("page", page)
            blocks.append(row)
    return blocks


def _packet_geometries(
    packets: list[dict[str, Any]],
    *,
    stream: str,
) -> tuple[list[PacketGeometry], dict[str, float]]:
    geometries: list[PacketGeometry] = []
    page_width_by_image: dict[str, float] = {}
    image_size_cache: dict[str, tuple[int, int]] = {}
    for packet in packets:
        page = int(packet.get("page") or 0)
        image_path = str(packet.get("page_image") or "")
        if not image_path:
            continue
        width, height = _image_size(image_path, image_size_cache)
        page_width_by_image[image_path] = float(width)
        for block in packet.get("blocks") or []:
            bbox = block.get("bbox")
            label = str(block.get("label") or "")
            if not label or not isinstance(bbox, list) or len(bbox) < 4:
                continue
            geometries.append(
                PacketGeometry(
                    label=label,
                    page=page,
                    stream=stream,
                    image_path=image_path,
                    bbox=_scale_bbox_to_pixels(bbox, width=width, height=height),
                    reading_order=int(block.get("reading_order") or block.get("order") or 0),
                )
            )
    return geometries, page_width_by_image


def _scale_bbox_to_pixels(
    bbox: list[float],
    *,
    width: int,
    height: int,
    coord_width: float = 1000.0,
    coord_height: float = 1000.0,
) -> tuple[float, float, float, float]:
    left, top, right, bottom = [float(value) for value in bbox]
    return (
        left / coord_width * width,
        top / coord_height * height,
        right / coord_width * width,
        bottom / coord_height * height,
    )


def _write_crop_image(
    *,
    crop_dir: Path,
    run_id: str,
    question_no: int,
    role: str,
    index: int,
    island: Any,
    image_cache: dict[str, tuple[int, int]],
) -> Path:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to render Step2 crop images") from exc

    width, height = _image_size(island.image_path, image_cache)
    left = max(0, int(round(island.bbox[0])))
    top = max(0, int(round(island.bbox[1])))
    right = min(width, int(round(island.bbox[2])))
    bottom = min(height, int(round(island.bbox[3])))
    if right <= left or bottom <= top:
        raise ValidationError(
            f"Invalid crop bbox for q{question_no} {role} page {island.page}: {island.bbox}"
        )
    filename = f"{run_id}_q{question_no:03d}_{role}_surface_p{island.page:03d}_{index}.png"
    out_path = crop_dir / filename
    with Image.open(island.image_path) as image:
        image.crop((left, top, right, bottom)).save(out_path)
    return out_path


def _image_size(path_text: str, cache: dict[str, tuple[int, int]]) -> tuple[int, int]:
    if path_text in cache:
        return cache[path_text]
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to render Step2 crop images") from exc

    path = Path(path_text)
    with Image.open(path) as image:
        cache[path_text] = (image.width, image.height)
    return cache[path_text]


def _unique_answer_labels(row: Any) -> list[str]:
    labels: list[str] = []
    for item in row.answer.items:
        for label in item.labels:
            if label not in labels:
                labels.append(label)
    return labels
