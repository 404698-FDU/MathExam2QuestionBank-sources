from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any

from exam_import.core.io import read_json
from exam_import.core.run_context import RunContext


def prepare_step2_crop_review_assets(*, run_context: RunContext) -> dict[str, Any]:
    manifest_path = run_context.step2_run_dir / "crops_manifest.json"
    if not manifest_path.exists():
        return {
            "question_crop_count": 0,
            "answer_crop_count": 0,
            "copied_question_crop_count": 0,
            "copied_answer_crop_count": 0,
            "missing_crop_count": 0,
            "by_question_no": {},
        }

    payload = read_json(manifest_path)
    crop_dir = run_context.render_dir / "step2_crops"
    if crop_dir.exists():
        shutil.rmtree(crop_dir)
    crop_dir.mkdir(parents=True, exist_ok=True)

    by_question_no: dict[int, dict[str, list[str]]] = {}
    copied_question, missing = _copy_crop_group(
        crop_map=payload.get("question_crops") or {},
        role="question",
        crop_dir=crop_dir,
        by_question_no=by_question_no,
    )
    copied_answer, missing = _copy_crop_group(
        crop_map=payload.get("answer_crops") or {},
        role="answer",
        crop_dir=crop_dir,
        by_question_no=by_question_no,
        missing_count=missing,
    )

    return {
        "question_crop_count": int(payload.get("question_crop_count") or 0),
        "answer_crop_count": int(payload.get("answer_crop_count") or 0),
        "copied_question_crop_count": copied_question,
        "copied_answer_crop_count": copied_answer,
        "missing_crop_count": missing,
        "by_question_no": by_question_no,
    }


def _copy_crop_group(
    *,
    crop_map: Any,
    role: str,
    crop_dir: Path,
    by_question_no: dict[int, dict[str, list[str]]],
    missing_count: int = 0,
) -> tuple[int, int]:
    copied_count = 0
    if not isinstance(crop_map, dict):
        return copied_count, missing_count
    for qno_text, crop_paths in crop_map.items():
        try:
            qno = int(qno_text)
        except (TypeError, ValueError):
            continue
        if not isinstance(crop_paths, list):
            continue
        bucket = by_question_no.setdefault(qno, {"question": [], "answer": []})
        for source_text in crop_paths:
            source = Path(str(source_text))
            if not source.exists() or not source.is_file():
                missing_count += 1
                continue
            dest = crop_dir / source.name
            shutil.copy2(source, dest)
            bucket[role].append(Path("step2_crops", dest.name).as_posix())
            copied_count += 1
    return copied_count, missing_count
