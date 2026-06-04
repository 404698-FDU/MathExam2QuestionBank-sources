from __future__ import annotations

from typing import Any

from exam_import.core.run_context import RunContext
from exam_import.schemas.answer_table import AnswerTableReview
from exam_import.schemas.question_record import QuestionRecord
from exam_import.schemas.visual_asset import VisualAssetReview

from .step4_assets import Step4SyncSummary, apply_step4_sync, write_step4_outputs


def sync_step4_results(
    *,
    run_context: RunContext,
    records: list[QuestionRecord],
    visual_review: VisualAssetReview,
    answer_table_review: AnswerTableReview | None,
) -> tuple[list[QuestionRecord], Step4SyncSummary, dict[str, Any]]:
    merged_records, sync_summary = apply_step4_sync(
        records=records,
        visual_review=visual_review,
        answer_table_review=answer_table_review,
    )
    outputs = write_step4_outputs(
        run_context=run_context,
        visual_review=visual_review,
        answer_table_review=answer_table_review,
        merged_records=merged_records,
        sync_summary=sync_summary,
    )
    return merged_records, sync_summary, outputs
