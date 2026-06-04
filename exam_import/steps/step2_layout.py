from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from exam_import.schemas.common import IssueRecord, ValidationError
from exam_import.schemas.pipeline_summary import PipelineSummary
from exam_import.schemas.qa_alignment import (
    ALIGNMENT_MODES,
    AnswerImport,
    AnswerItem,
    AnswerSide,
    QAAlignmentDocument,
    QAAlignmentRow,
    QuestionSide,
    VisualAssetSummary,
)


@dataclass(frozen=True)
class PacketBlock:
    label: str
    page: int
    order: int
    kind: str
    bbox: tuple[float, float, float, float] | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PacketBlock":
        label = str(payload.get("label") or "").strip()
        if not label:
            raise ValidationError("packet block label must not be empty")
        page = int(payload.get("page") or 0)
        order = int(payload.get("order") or payload.get("reading_order") or 0)
        kind = str(payload.get("kind") or payload.get("type") or "text").strip() or "text"
        bbox_value = payload.get("bbox")
        bbox: tuple[float, float, float, float] | None = None
        if isinstance(bbox_value, list) and len(bbox_value) >= 4:
            bbox = (
                float(bbox_value[0]),
                float(bbox_value[1]),
                float(bbox_value[2]),
                float(bbox_value[3]),
            )
        return cls(label=label, page=page, order=order, kind=kind, bbox=bbox)


@dataclass(frozen=True)
class RangeResult:
    question_no: int
    start_label: str
    end_label: str
    visual_labels: list[str]
    confidence: float
    reason: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RangeResult":
        question_no = int(payload.get("question_no") or 0)
        if question_no < 1:
            raise ValidationError("question_no must be a positive integer")
        start_label = str(payload.get("start_label") or "").strip()
        end_label = str(payload.get("end_label") or "").strip()
        if not start_label or not end_label:
            raise ValidationError("start_label and end_label must not be empty")
        visual_labels = [str(item) for item in payload.get("visual_labels") or [] if str(item)]
        confidence = float(payload.get("confidence") or 0)
        reason = str(payload.get("reason") or "").strip()
        return cls(
            question_no=question_no,
            start_label=start_label,
            end_label=end_label,
            visual_labels=visual_labels,
            confidence=confidence,
            reason=reason,
        )


def build_qa_alignment_document(
    *,
    run_id: str,
    alignment_mode: str,
    question_ranges: list[Mapping[str, Any]],
    question_packets: list[Mapping[str, Any]],
    answer_ranges: list[Mapping[str, Any]] | None = None,
    answer_packets: list[Mapping[str, Any]] | None = None,
    existing_alignment: QAAlignmentDocument | None = None,
) -> QAAlignmentDocument:
    if alignment_mode not in ALIGNMENT_MODES:
        raise ValidationError(f"Unsupported alignment_mode: {alignment_mode}")
    question_blocks = _sorted_blocks(question_packets)
    question_index = {block.label: block for block in question_blocks}
    question_ranges_typed = [RangeResult.from_dict(item) for item in question_ranges]
    if not question_ranges_typed and alignment_mode != "answer_patch":
        raise ValidationError("question_ranges must not be empty")

    answer_ranges_typed = [RangeResult.from_dict(item) for item in (answer_ranges or [])]
    answer_blocks = _sorted_blocks(answer_packets or [])

    if alignment_mode == "answer_patch":
        if existing_alignment is None:
            raise ValidationError("answer_patch requires existing_alignment")
        rows, extra_numbers, missing_numbers = _build_answer_patch_rows(
            existing_alignment=existing_alignment,
            answer_ranges=answer_ranges_typed,
            answer_blocks=answer_blocks,
        )
        answer_import = AnswerImport(
            status="patched",
            preserves_question_side=True,
            source_run_id=existing_alignment.run_id,
            answer_pdf="",
            patched_at="",
        )
        source_parts = ["paper", "answer"]
    elif alignment_mode == "mixed":
        rows = [
            _build_mixed_row(range_result=item, question_blocks=question_blocks, question_index=question_index)
            for item in question_ranges_typed
        ]
        extra_numbers = []
        missing_numbers = []
        answer_import = AnswerImport(
            status="not_applicable",
            preserves_question_side=False,
            source_run_id="",
            answer_pdf="",
            patched_at="",
        )
        source_parts = ["mixed"]
    else:
        answer_lookup = {item.question_no: item for item in answer_ranges_typed}
        rows = [
            _build_separate_row(
                alignment_mode=alignment_mode,
                range_result=item,
                question_blocks=question_blocks,
                question_index=question_index,
                answer_range=answer_lookup.get(item.question_no),
                answer_blocks=answer_blocks,
            )
            for item in question_ranges_typed
        ]
        question_numbers = {item.question_no for item in question_ranges_typed}
        answer_numbers = {item.question_no for item in answer_ranges_typed}
        extra_numbers = sorted(answer_numbers - question_numbers)
        if alignment_mode == "pure_paper":
            missing_numbers = sorted(question_numbers)
            answer_import = AnswerImport(
                status="pending",
                preserves_question_side=False,
                source_run_id="",
                answer_pdf="",
                patched_at="",
            )
            source_parts = ["paper"]
        else:
            missing_numbers = sorted(
                row.question_no for row in rows if row.answer.status == "missing"
            )
            answer_import = AnswerImport(
                status="initial_import",
                preserves_question_side=False,
                source_run_id="",
                answer_pdf="",
                patched_at="",
            )
            source_parts = ["paper", "answer"]

    return QAAlignmentDocument(
        schema_version="qa_alignment_v2",
        run_id=run_id,
        alignment_mode=alignment_mode,
        source_parts=source_parts,
        qa_alignment=rows,
        extra_answer_numbers=extra_numbers,
        missing_answer_numbers=missing_numbers,
        answer_import=answer_import,
        visual_asset_assignment_summary=VisualAssetSummary(
            asset_count=0,
            assigned_count=0,
            noise_count=0,
            uncertain_count=0,
            risk_count=0,
        ),
        asset_match_risk_count=0,
    )


def build_pipeline_summary(document: QAAlignmentDocument) -> PipelineSummary:
    answered_question_count = sum(
        1 for row in document.qa_alignment if row.answer.status in {"found", "embedded"}
    )
    return PipelineSummary(
        run_id=document.run_id,
        alignment_mode=document.alignment_mode,
        question_count=len(document.qa_alignment),
        answered_question_count=answered_question_count,
        missing_answer_numbers=document.missing_answer_numbers,
        extra_answer_numbers=document.extra_answer_numbers,
    )


def _sorted_blocks(raw_packets: list[Mapping[str, Any]]) -> list[PacketBlock]:
    blocks = [PacketBlock.from_dict(item) for item in raw_packets]
    return sorted(blocks, key=lambda item: (item.page, item.order, item.label))


def _labels_for_range(
    *,
    range_result: RangeResult,
    blocks: list[PacketBlock],
) -> list[str]:
    ordered_labels = [item.label for item in blocks]
    try:
        start_index = ordered_labels.index(range_result.start_label)
        end_index = ordered_labels.index(range_result.end_label)
    except ValueError as exc:
        raise ValidationError(
            f"Range labels not found for q{range_result.question_no}: {range_result.start_label} -> {range_result.end_label}"
        ) from exc
    if start_index > end_index:
        raise ValidationError(
            f"Range start/end order invalid for q{range_result.question_no}: {range_result.start_label} -> {range_result.end_label}"
        )
    return ordered_labels[start_index : end_index + 1]


def _surface_labels(labels: list[str], question_index: dict[str, PacketBlock]) -> list[str]:
    result: list[str] = []
    for label in labels:
        block = question_index.get(label)
        kind = (block.kind or "").lower() if block else ""
        if "image" in kind or "table" in kind or "chart" in kind:
            continue
        result.append(label)
    return result


def _merge_labels(primary: list[str], extra: list[str], block_order: dict[str, tuple[int, int, str]]) -> list[str]:
    merged = list(dict.fromkeys(primary + extra))
    return sorted(merged, key=lambda label: block_order.get(label, (10**9, 10**9, label)))


def _block_order_map(blocks: list[PacketBlock]) -> dict[str, tuple[int, int, str]]:
    return {item.label: (item.page, item.order, item.label) for item in blocks}


def _build_mixed_row(
    *,
    range_result: RangeResult,
    question_blocks: list[PacketBlock],
    question_index: dict[str, PacketBlock],
) -> QAAlignmentRow:
    core_labels = _labels_for_range(range_result=range_result, blocks=question_blocks)
    block_order = _block_order_map(question_blocks)
    question_labels = _merge_labels(core_labels, range_result.visual_labels, block_order)
    question = QuestionSide(
        source_part="mixed",
        range_mode="mixed",
        status="found" if question_labels else "missing",
        labels=question_labels,
        core_labels=core_labels,
        surface_labels=_surface_labels(core_labels, question_index),
        visual_labels=range_result.visual_labels,
    )
    answer = AnswerSide(
        status="embedded",
        source_mode="mixed",
        source_part="mixed",
        embedded_in_question_range=True,
        items=[],
    )
    return QAAlignmentRow(
        question_no=range_result.question_no,
        question=question,
        answer=answer,
        alignment_status="mixed_embedded",
        issues=[],
    )


def _build_separate_row(
    *,
    alignment_mode: str,
    range_result: RangeResult,
    question_blocks: list[PacketBlock],
    question_index: dict[str, PacketBlock],
    answer_range: RangeResult | None,
    answer_blocks: list[PacketBlock],
) -> QAAlignmentRow:
    core_labels = _labels_for_range(range_result=range_result, blocks=question_blocks)
    block_order = _block_order_map(question_blocks)
    question_labels = _merge_labels(core_labels, range_result.visual_labels, block_order)
    question = QuestionSide(
        source_part="paper",
        range_mode="pure_paper",
        status="found" if question_labels else "missing",
        labels=question_labels,
        core_labels=core_labels,
        surface_labels=_surface_labels(core_labels, question_index),
        visual_labels=range_result.visual_labels,
    )
    if alignment_mode == "pure_paper":
        answer = AnswerSide(
            status="pending_import",
            source_mode="none",
            source_part="none",
            embedded_in_question_range=False,
            items=[],
        )
        alignment_status = "question_only"
    elif answer_range is None:
        answer = AnswerSide(
            status="missing",
            source_mode="pure_answer",
            source_part="answer",
            embedded_in_question_range=False,
            items=[],
        )
        alignment_status = "needs_review"
    else:
        answer_labels = _labels_for_range(range_result=answer_range, blocks=answer_blocks)
        answer = AnswerSide(
            status="found",
            source_mode="pure_answer",
            source_part="answer",
            embedded_in_question_range=False,
            items=[
                AnswerItem(
                    role="answer",
                    range_mode="pure_answer",
                    labels=answer_labels,
                    span_text_excerpt="",
                    continues_previous=False,
                    confidence=answer_range.confidence,
                    reason=answer_range.reason or "Step2 pure_answer range matched by question_no.",
                )
            ],
        )
        alignment_status = "question_with_answer"
    return QAAlignmentRow(
        question_no=range_result.question_no,
        question=question,
        answer=answer,
        alignment_status=alignment_status,
        issues=[],
    )


def _build_answer_patch_rows(
    *,
    existing_alignment: QAAlignmentDocument,
    answer_ranges: list[RangeResult],
    answer_blocks: list[PacketBlock],
) -> tuple[list[QAAlignmentRow], list[int], list[int]]:
    answer_lookup = {item.question_no: item for item in answer_ranges}
    question_numbers = {row.question_no for row in existing_alignment.qa_alignment}
    extra_numbers = sorted(set(answer_lookup) - question_numbers)
    rows: list[QAAlignmentRow] = []
    for row in existing_alignment.qa_alignment:
        answer_range = answer_lookup.get(row.question_no)
        if answer_range is None:
            answer = AnswerSide(
                status="missing",
                source_mode="answer_patch",
                source_part="answer",
                embedded_in_question_range=False,
                items=[],
            )
            alignment_status = "needs_review"
        else:
            answer_labels = _labels_for_range(range_result=answer_range, blocks=answer_blocks)
            answer = AnswerSide(
                status="found",
                source_mode="answer_patch",
                source_part="answer",
                embedded_in_question_range=False,
                items=[
                    AnswerItem(
                        role="answer",
                        range_mode="pure_answer",
                        labels=answer_labels,
                        span_text_excerpt="",
                        continues_previous=False,
                        confidence=answer_range.confidence,
                        reason=answer_range.reason or "Step2 answer_patch range matched by question_no.",
                    )
                ],
            )
            alignment_status = "answer_patched"
        rows.append(
            QAAlignmentRow(
                question_no=row.question_no,
                question=row.question,
                answer=answer,
                alignment_status=alignment_status,
                issues=row.issues,
            )
        )
    missing_numbers = sorted(row.question_no for row in rows if row.answer.status == "missing")
    return rows, extra_numbers, missing_numbers
