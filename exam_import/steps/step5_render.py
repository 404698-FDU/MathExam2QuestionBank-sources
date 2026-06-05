from __future__ import annotations

from pathlib import Path
from typing import Any

from exam_import.core.io import write_json, write_text
from exam_import.core.run_context import RunContext
from exam_import.render import AssetResolver, build_run_index_html, export_render_assets, render_question_record
from exam_import.render.step2_crop_review import prepare_step2_crop_review_assets
from exam_import.schemas.pipeline_summary import PipelineSummary
from exam_import.schemas.question_record import QuestionRecord


def render_question_bank(
    *,
    run_context: RunContext,
    records: list[QuestionRecord],
    pipeline_summary: PipelineSummary | None = None,
) -> dict[str, Any]:
    run_dir = run_context.render_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    step2_crop_review = prepare_step2_crop_review_assets(run_context=run_context)
    asset_root = run_dir / "assets"
    asset_export = export_render_assets(
        run_context=run_context,
        records=records,
        asset_root=asset_root,
    )
    resolver = AssetResolver(asset_root=asset_root)
    sorted_records = sorted(records, key=lambda item: item.question_no)
    crop_lookup = step2_crop_review.get("by_question_no") or {}
    body_html = "".join(
        render_question_record(record, resolver, step2_crops=crop_lookup.get(record.question_no))
        for record in sorted_records
    )
    nav_html = "".join(
        f'<a href="#q{record.question_no:03d}">Q{record.question_no}</a>'
        for record in sorted_records
    )
    index_html = build_run_index_html(run_id=run_context.run_id, body_html=body_html, nav_html=nav_html)
    write_text(run_dir / "index.html", index_html)
    root_index = run_context.render_root / "index.html"
    write_text(
        root_index,
        build_run_index_html(
            run_id="rendered_question_bank_mathjax",
            body_html=f'<article class="question-card"><p><a href="{run_context.run_id}/index.html">{run_context.run_id}</a></p></article>',
            nav_html="",
        ),
    )
    summary = {
        "run_id": run_context.run_id,
        "alignment_mode": run_context.alignment_mode,
        "question_count": len(records),
        "render_index": str(run_dir / "index.html"),
        "assets_manifest": str(run_dir / "assets_manifest.json"),
        "asset_export": asset_export,
        "step2_crop_review": {
            "question_crop_count": int(step2_crop_review.get("question_crop_count") or 0),
            "answer_crop_count": int(step2_crop_review.get("answer_crop_count") or 0),
            "copied_question_crop_count": int(step2_crop_review.get("copied_question_crop_count") or 0),
            "copied_answer_crop_count": int(step2_crop_review.get("copied_answer_crop_count") or 0),
            "missing_crop_count": int(step2_crop_review.get("missing_crop_count") or 0),
        },
    }
    if pipeline_summary is not None:
        summary["pipeline_summary"] = pipeline_summary.to_dict()
    write_json(run_dir / "summary.json", summary)
    return {
        "index": str(root_index),
        "runs": [str(run_dir / "index.html")],
        "assets_manifest": str(run_dir / "assets_manifest.json"),
        "asset_export": asset_export,
        "step2_crop_review": {
            "question_crop_count": int(step2_crop_review.get("question_crop_count") or 0),
            "answer_crop_count": int(step2_crop_review.get("answer_crop_count") or 0),
            "copied_question_crop_count": int(step2_crop_review.get("copied_question_crop_count") or 0),
            "copied_answer_crop_count": int(step2_crop_review.get("copied_answer_crop_count") or 0),
            "missing_crop_count": int(step2_crop_review.get("missing_crop_count") or 0),
        },
    }
