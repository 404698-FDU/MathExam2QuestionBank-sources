from __future__ import annotations

from pathlib import Path
from typing import Any

from exam_import.core.io import write_json, write_text
from exam_import.core.run_context import RunContext
from exam_import.render import AssetResolver, build_run_index_html, export_render_assets, render_question_record
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
    asset_root = run_dir / "assets"
    asset_export = export_render_assets(
        run_context=run_context,
        records=records,
        asset_root=asset_root,
    )
    resolver = AssetResolver(asset_root=asset_root)
    body_html = "".join(render_question_record(record, resolver) for record in sorted(records, key=lambda item: item.question_no))
    index_html = build_run_index_html(run_id=run_context.run_id, body_html=body_html)
    write_text(run_dir / "index.html", index_html)
    root_index = run_context.render_root / "index.html"
    write_text(
        root_index,
        build_run_index_html(
            run_id="rendered_question_bank_mathjax",
            body_html=f'<article class="question-card"><p><a href="{run_context.run_id}/index.html">{run_context.run_id}</a></p></article>',
        ),
    )
    summary = {
        "run_id": run_context.run_id,
        "alignment_mode": run_context.alignment_mode,
        "question_count": len(records),
        "render_index": str(run_dir / "index.html"),
        "assets_manifest": str(run_dir / "assets_manifest.json"),
        "asset_export": asset_export,
    }
    if pipeline_summary is not None:
        summary["pipeline_summary"] = pipeline_summary.to_dict()
    write_json(run_dir / "summary.json", summary)
    return {
        "index": str(root_index),
        "runs": [str(run_dir / "index.html")],
        "assets_manifest": str(run_dir / "assets_manifest.json"),
        "asset_export": asset_export,
    }
