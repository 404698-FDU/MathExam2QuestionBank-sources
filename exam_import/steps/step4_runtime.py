from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from exam_import.core.io import read_json, write_json
from exam_import.core.run_context import RunContext
from exam_import.prompts.loader import PromptLoader
from exam_import.schemas.answer_table import AnswerTableReview
from exam_import.schemas.qa_alignment import QAAlignmentDocument
from exam_import.schemas.question_ranges import QuestionRangesResult
from exam_import.schemas.question_record import QuestionRecord
from exam_import.schemas.visual_asset import VisualAssetReview
from exam_import.sources.ocr_blocks import load_packets
from .step4_assets import call_answer_table_review, call_visual_asset_review
from .step45_sync import sync_step4_results


PLACEHOLDER_RE = re.compile(r"<(img|table|chart)\s+src=\"([^\"]+)\">")


@dataclass(frozen=True)
class Step4RunResult:
    visual_review: VisualAssetReview
    answer_table_review: AnswerTableReview | None
    merged_records: list[QuestionRecord]
    summary: dict[str, Any]


@dataclass(frozen=True)
class Step4VisualJob:
    compact_payload: dict[str, Any]
    annotated_page_image: str


@dataclass(frozen=True)
class Step4AnswerTableJob:
    compact_payload: dict[str, Any]
    annotated_page_image: str


def run_step4(
    *,
    run_context: RunContext,
    qa_alignment: QAAlignmentDocument,
    records: list[QuestionRecord],
    visual_call_spec_path,
    answer_table_call_spec_path=None,
    prompt_loader: PromptLoader | None = None,
    visual_client=None,
    answer_table_client=None,
    env: dict[str, str] | None = None,
) -> Step4RunResult:
    prompt_loader = prompt_loader or PromptLoader()
    visual_jobs, answer_jobs = _build_step4_jobs(
        run_context=run_context,
        qa_alignment=qa_alignment,
        records=records,
    )
    visual_inputs = [job.compact_payload for job in visual_jobs]
    answer_inputs = [job.compact_payload for job in answer_jobs]
    write_json(run_context.step2_run_dir / "visual_asset_assignment_input_compact.json", visual_inputs)
    write_json(run_context.step2_run_dir / "answer_table_extraction_input_compact.json", answer_inputs)

    visual_assets: list[dict[str, Any]] = []
    visual_risks: list[dict[str, Any]] = []
    for job in visual_jobs:
        review = call_visual_asset_review(
            compact_payload=job.compact_payload,
            annotated_page_image=Path(job.annotated_page_image),
            call_spec_path=visual_call_spec_path,
            client=visual_client,
            prompt_loader=prompt_loader,
            env=env,
        )
        visual_assets.extend(item.to_dict() for item in review.assets)
        visual_risks.extend(item.to_dict() for item in review.risks)
    visual_review = VisualAssetReview.from_dict({"assets": visual_assets, "risks": visual_risks})

    answer_table_review: AnswerTableReview | None = None
    if answer_inputs and answer_table_call_spec_path is None:
        raise RuntimeError("Step4 answer table inputs exist, but llm.call_specs['step4_answer_tables'] is missing")
    if answer_inputs and answer_table_call_spec_path is not None:
        tables: list[dict[str, Any]] = []
        risks: list[dict[str, Any]] = []
        for job in answer_jobs:
            review = call_answer_table_review(
                compact_payload=job.compact_payload,
                annotated_page_image=Path(job.annotated_page_image),
                call_spec_path=answer_table_call_spec_path,
                client=answer_table_client,
                prompt_loader=prompt_loader,
                env=env,
            )
            tables.extend(item.to_dict() for item in review.tables)
            risks.extend(item.to_dict() for item in review.risks)
        answer_table_review = AnswerTableReview.from_dict({"tables": tables, "risks": risks})

    merged_records, sync_summary, sync_outputs = sync_step4_results(
        run_context=run_context,
        records=records,
        visual_review=visual_review,
        answer_table_review=answer_table_review,
    )
    summary = {
        **sync_summary.to_dict(),
        "asset_count": len(visual_review.assets),
        "risk_count": len(visual_review.risks),
        "answer_table_count": len(answer_table_review.tables) if answer_table_review else 0,
        "answer_table_risk_count": len(answer_table_review.risks) if answer_table_review else 0,
        "sync_output": sync_outputs,
    }
    return Step4RunResult(
        visual_review=visual_review,
        answer_table_review=answer_table_review,
        merged_records=merged_records,
        summary=summary,
    )


def build_step4_inputs(
    *,
    run_context: RunContext,
    qa_alignment: QAAlignmentDocument,
    records: list[QuestionRecord],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    visual_jobs, answer_jobs = _build_step4_jobs(
        run_context=run_context,
        qa_alignment=qa_alignment,
        records=records,
    )
    return (
        [job.compact_payload for job in visual_jobs],
        [job.compact_payload for job in answer_jobs],
    )


def _build_step4_jobs(
    *,
    run_context: RunContext,
    qa_alignment: QAAlignmentDocument,
    records: list[QuestionRecord],
) -> tuple[list[Step4VisualJob], list[Step4AnswerTableJob]]:
    question_packets = load_packets(run_context.step2_run_dir / "question_packets")
    answer_packets = load_packets(run_context.step2_run_dir / "answer_packets")
    question_source_part = "mixed" if run_context.alignment_mode == "mixed" else "paper"
    range_by_source = _load_step2_ranges(run_context)
    record_by_qno = {record.question_no: record for record in records}
    refs_by_label = _placeholder_refs_by_label(records)
    visual_jobs: list[Step4VisualJob] = []
    answer_jobs: list[Step4AnswerTableJob] = []

    if question_packets:
        for packet in question_packets:
            payload = _build_visual_asset_payload(
                run_context=run_context,
                qa_alignment=qa_alignment,
                packet=packet,
                source_part=question_source_part,
                range_mode="mixed" if run_context.alignment_mode == "mixed" else "pure_paper",
                range_by_qno=range_by_source.get(question_source_part, {}),
                record_by_qno=record_by_qno,
                refs_by_label=refs_by_label,
            )
            if payload["assets"]:
                visual_jobs.append(
                    Step4VisualJob(
                        compact_payload=payload,
                        annotated_page_image=str(packet.get("annotated_page_image") or ""),
                    )
                )

    if answer_packets:
        for packet in answer_packets:
            visual_payload = _build_visual_asset_payload(
                run_context=run_context,
                qa_alignment=qa_alignment,
                packet=packet,
                source_part="answer",
                range_mode="pure_answer",
                range_by_qno=range_by_source.get("answer", {}),
                record_by_qno=record_by_qno,
                refs_by_label=refs_by_label,
            )
            if visual_payload["assets"]:
                visual_jobs.append(
                    Step4VisualJob(
                        compact_payload=visual_payload,
                        annotated_page_image=str(packet.get("annotated_page_image") or ""),
                    )
                )
            table_payload = _build_answer_table_payload(
                run_context=run_context,
                qa_alignment=qa_alignment,
                packet=packet,
                range_by_qno=range_by_source.get("answer", {}),
                record_by_qno=record_by_qno,
            )
            if table_payload["tables"]:
                answer_jobs.append(
                    Step4AnswerTableJob(
                        compact_payload=table_payload,
                        annotated_page_image=str(packet.get("annotated_page_image") or ""),
                    )
                )

    return visual_jobs, answer_jobs


def _build_visual_asset_payload(
    *,
    run_context: RunContext,
    qa_alignment: QAAlignmentDocument,
    packet: dict[str, Any],
    source_part: str,
    range_mode: str,
    range_by_qno: dict[int, dict[str, Any]],
    record_by_qno: dict[int, QuestionRecord],
    refs_by_label: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    page = int(packet.get("page") or 0)
    page_qnos = _page_question_numbers(qa_alignment, source_part=source_part, page=page)
    step2_question_ranges = [
        range_by_qno[qno]
        for qno in page_qnos
        if qno in range_by_qno
    ]
    step3_records = [
        record_by_qno[qno].to_dict()
        for qno in page_qnos
        if qno in record_by_qno
    ]
    assets: list[dict[str, Any]] = []
    for block in packet.get("blocks") or []:
        kind = str(block.get("kind") or block.get("type") or "").lower()
        if kind not in {"image", "table", "chart"}:
            continue
        label = str(block.get("label") or "")
        if not label:
            continue
        candidate_qnos = _candidate_qnos_for_asset(
            qa_alignment=qa_alignment,
            source_part=source_part,
            label=label,
            page_qnos=page_qnos,
        )
        relation = _step2_relation_for_asset(
            qa_alignment=qa_alignment,
            source_part=source_part,
            label=label,
            candidate_qnos=candidate_qnos,
        )
        assets.append(
            {
                "label": label,
                "kind": kind,
                "candidate_qnos": candidate_qnos,
                "candidate_fields": _candidate_fields_for_source(source_part),
                "asset_part": source_part,
                "step2_relation": relation,
                "step2_visual_label": relation == "visual_label",
                "step3_placeholder_refs": refs_by_label.get(label, []),
            }
        )
    return {
        "run_id": run_context.run_id,
        "range_mode": range_mode,
        "source_part": source_part,
        "page": page,
        "step2_question_ranges": step2_question_ranges,
        "step3_records": step3_records,
        "assets": assets,
    }


def _build_answer_table_payload(
    *,
    run_context: RunContext,
    qa_alignment: QAAlignmentDocument,
    packet: dict[str, Any],
    range_by_qno: dict[int, dict[str, Any]],
    record_by_qno: dict[int, QuestionRecord],
) -> dict[str, Any]:
    page = int(packet.get("page") or 0)
    page_qnos = _page_question_numbers(qa_alignment, source_part="answer", page=page)
    step2_question_ranges = [
        range_by_qno[qno]
        for qno in page_qnos
        if qno in range_by_qno
    ]
    step3_records = [
        record_by_qno[qno].to_dict()
        for qno in page_qnos
        if qno in record_by_qno
    ]
    tables: list[dict[str, Any]] = []
    for block in packet.get("blocks") or []:
        kind = str(block.get("kind") or block.get("type") or "").lower()
        if kind != "table":
            continue
        label = str(block.get("label") or "")
        if not label:
            continue
        tables.append(
            {
                "label": label,
                "html": str(block.get("text") or ""),
                "candidate_qnos": page_qnos,
            }
        )
    return {
        "run_id": run_context.run_id,
        "range_mode": "pure_answer",
        "source_part": "answer",
        "page": page,
        "step2_question_ranges": step2_question_ranges,
        "step3_records": step3_records,
        "tables": tables,
    }


def _load_step2_ranges(run_context: RunContext) -> dict[str, dict[int, dict[str, Any]]]:
    result: dict[str, dict[int, dict[str, Any]]] = {}
    mapping = {
        "paper": run_context.step2_run_dir / "step2_pure_paper_ranges.json",
        "mixed": run_context.step2_run_dir / "step2_mixed_ranges.json",
        "answer": run_context.step2_run_dir / "step2_pure_answer_ranges.json",
    }
    for source_part, path in mapping.items():
        if not path.exists():
            continue
        payload = QuestionRangesResult.from_dict(read_json(path))
        result[source_part] = {
            item.question_no: {
                "question_no": item.question_no,
                "start_label": item.start_label,
                "end_label": item.end_label,
                "visual_labels": item.visual_labels,
            }
            for item in payload.question_ranges
        }
    return result


def _page_question_numbers(
    qa_alignment: QAAlignmentDocument,
    *,
    source_part: str,
    page: int,
) -> list[int]:
    page_qnos: list[int] = []
    for row in qa_alignment.qa_alignment:
        if source_part == "answer":
            labels = [label for item in row.answer.items for label in item.labels]
        else:
            labels = row.question.labels
        if any(_page_from_label(label) == page for label in labels):
            page_qnos.append(row.question_no)
    return sorted(dict.fromkeys(page_qnos))


def _candidate_qnos_for_asset(
    *,
    qa_alignment: QAAlignmentDocument,
    source_part: str,
    label: str,
    page_qnos: list[int],
) -> list[int]:
    candidate_qnos: list[int] = []
    for row in qa_alignment.qa_alignment:
        labels = [label for item in row.answer.items for label in item.labels] if source_part == "answer" else row.question.labels
        if label in labels:
            candidate_qnos.append(row.question_no)
    if candidate_qnos:
        return sorted(dict.fromkeys(candidate_qnos))
    return page_qnos


def _step2_relation_for_asset(
    *,
    qa_alignment: QAAlignmentDocument,
    source_part: str,
    label: str,
    candidate_qnos: list[int],
) -> str:
    for row in qa_alignment.qa_alignment:
        if row.question_no not in candidate_qnos:
            continue
        if source_part != "answer" and label in row.question.visual_labels:
            return "visual_label"
        if source_part == "answer":
            if any(label in item.labels for item in row.answer.items):
                return "in_range"
        elif label in row.question.labels:
            return "in_range"
    return "outside_nearest" if candidate_qnos else "orphan"


def _candidate_fields_for_source(source_part: str) -> list[str]:
    if source_part == "paper":
        return ["stem_markdown", "options_markdown"]
    if source_part == "answer":
        return ["answer_markdown", "analysis_markdown"]
    return ["stem_markdown", "options_markdown", "answer_markdown", "analysis_markdown"]


def _placeholder_refs_by_label(records: list[QuestionRecord]) -> dict[str, list[dict[str, Any]]]:
    refs: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for field_name, segments in (
            ("stem_markdown", record.stem_markdown),
            ("answer_markdown", record.answer_markdown),
            ("analysis_markdown", record.analysis_markdown),
        ):
            for segment in segments:
                _collect_refs(refs, record.question_no, field_name, "", "", segment)
        for group in record.options_markdown:
            for option in group.options:
                for segment in option.content_markdown:
                    _collect_refs(
                        refs,
                        record.question_no,
                        "options_markdown",
                        group.no,
                        option.label,
                        segment,
                    )
    return refs


def _collect_refs(
    refs: dict[str, list[dict[str, Any]]],
    question_no: int,
    field: str,
    option_group_no: str,
    option_label: str,
    segment: str,
) -> None:
    for match in PLACEHOLDER_RE.finditer(segment):
        label = match.group(2)
        refs.setdefault(label, []).append(
            {
                "question_no": question_no,
                "field": field,
                "option_group_no": option_group_no,
                "option_label": option_label,
                "placeholder": match.group(0),
            }
        )


def _page_from_label(label: str) -> int:
    match = re.search(r"-V(\d+)-", label)
    if not match:
        return 0
    return int(match.group(1))
