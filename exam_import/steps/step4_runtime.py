from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Any

from exam_import.core.execution import call_with_retries
from exam_import.core.io import read_json, write_json
from exam_import.core.run_context import RunContext
from exam_import.prompts.loader import PromptLoader
from exam_import.render.asset_export import SourceAssetRef, _resolve_source_block, _resolve_source_image_path
from exam_import.schemas.answer_table import AnswerTableReview
from exam_import.schemas.common import ValidationError
from exam_import.schemas.qa_alignment import QAAlignmentDocument
from exam_import.schemas.question_ranges import QuestionRangesResult
from exam_import.schemas.question_record import QuestionRecord
from exam_import.schemas.visual_asset import VisualAssetReview, VisualAssetRisk
from exam_import.sources.ocr_blocks import load_packets
from .step4_assets import call_answer_table_review, call_visual_asset_review
from .step45_sync import sync_step4_results


PLACEHOLDER_RE = re.compile(r"<(img|table|chart)\s+src=\"([^\"]+)\">")
LOW_SIGNAL_MIN_MEAN_LUMA = 248.0
LOW_SIGNAL_MAX_STD_LUMA = 12.0
LOW_SIGNAL_MAX_DARK_PIXEL_RATIO = 0.005


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
    max_workers: int = 1,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 2.0,
) -> Step4RunResult:
    started = time.monotonic()
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
    visual_reviews, visual_retry_count, visual_attempt_count = _run_job_group(
        jobs=visual_jobs,
        max_workers=max_workers,
        retry_attempts=retry_attempts,
        retry_delay_seconds=retry_delay_seconds,
        worker=lambda job: _run_visual_asset_job(
            job=job,
            visual_call_spec_path=visual_call_spec_path,
            visual_client=visual_client,
            prompt_loader=prompt_loader,
            env=env,
        ),
        describe_job=lambda job: f"visual page={int(job.compact_payload.get('page') or 0)}",
    )
    for job, review in visual_reviews:
        visual_assets.extend(item.to_dict() for item in review.assets)
        visual_risks.extend(item.to_dict() for item in review.risks)
    visual_review = VisualAssetReview.from_dict({"assets": visual_assets, "risks": visual_risks})

    answer_table_review: AnswerTableReview | None = None
    answer_retry_count = 0
    answer_attempt_count = 0
    if answer_inputs and answer_table_call_spec_path is None:
        raise RuntimeError("Step4 answer table inputs exist, but llm.call_specs['step4_answer_tables'] is missing")
    if answer_inputs and answer_table_call_spec_path is not None:
        tables: list[dict[str, Any]] = []
        risks: list[dict[str, Any]] = []
        answer_reviews, answer_retry_count, answer_attempt_count = _run_job_group(
            jobs=answer_jobs,
            max_workers=max_workers,
            retry_attempts=retry_attempts,
            retry_delay_seconds=retry_delay_seconds,
            worker=lambda job: call_answer_table_review(
                compact_payload=job.compact_payload,
                annotated_page_image=Path(job.annotated_page_image),
                call_spec_path=answer_table_call_spec_path,
                client=answer_table_client,
                prompt_loader=prompt_loader,
                env=env,
            ),
            describe_job=lambda job: f"answer_table page={int(job.compact_payload.get('page') or 0)}",
        )
        for _job, review in answer_reviews:
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
        "worker_count": max(1, min(max_workers, max(len(visual_jobs), len(answer_jobs), 1))),
        "retry_count": visual_retry_count + answer_retry_count,
        "attempt_count": visual_attempt_count + answer_attempt_count,
        "visual_job_count": len(visual_jobs),
        "answer_table_job_count": len(answer_jobs),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "sync_output": sync_outputs,
    }
    return Step4RunResult(
        visual_review=visual_review,
        answer_table_review=answer_table_review,
        merged_records=merged_records,
        summary=summary,
    )


def _run_job_group(
    *,
    jobs: list[Any],
    max_workers: int,
    retry_attempts: int,
    retry_delay_seconds: float,
    worker,
    describe_job,
) -> tuple[list[tuple[Any, Any]], int, int]:
    if not jobs:
        return [], 0, 0

    results: list[tuple[Any, Any]] = []
    retry_count = 0
    attempt_count = 0
    worker_limit = max(1, min(max_workers, len(jobs)))
    with ThreadPoolExecutor(max_workers=worker_limit) as executor:
        future_map = {
            executor.submit(
                _run_job_with_retries,
                job=job,
                worker=worker,
                retry_attempts=retry_attempts,
                retry_delay_seconds=retry_delay_seconds,
            ): job
            for job in jobs
        }
        for future in as_completed(future_map):
            job = future_map[future]
            retry_result = future.result()
            attempt_count += retry_result.attempts
            retry_count += max(0, retry_result.attempts - 1)
            if not retry_result.success:
                detail = describe_job(job)
                raise RuntimeError(
                    f"Step4 job failed after {retry_result.attempts} attempts: {detail}: {retry_result.error}"
                ) from retry_result.error
            results.append((job, retry_result.value))
    return results, retry_count, attempt_count


def _run_job_with_retries(
    *,
    job: Any,
    worker,
    retry_attempts: int,
    retry_delay_seconds: float,
):
    return call_with_retries(
        lambda: worker(job),
        max_attempts=retry_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )


def _run_visual_asset_job(
    *,
    job: Step4VisualJob,
    visual_call_spec_path,
    visual_client,
    prompt_loader: PromptLoader,
    env: dict[str, str] | None,
) -> VisualAssetReview:
    review = call_visual_asset_review(
        compact_payload=job.compact_payload,
        annotated_page_image=Path(job.annotated_page_image),
        call_spec_path=visual_call_spec_path,
        client=visual_client,
        prompt_loader=prompt_loader,
        env=env,
    )
    review = _filter_unmapped_visual_assets(review, job.compact_payload)
    review = _apply_deterministic_visual_asset_overrides(review, job.compact_payload)
    review = _normalize_unspecified_option_targets(review, job.compact_payload)
    _validate_visual_review_for_sync(review, job.compact_payload)
    return review


def _validate_visual_review_for_sync(review: VisualAssetReview, compact_payload: dict[str, Any]) -> None:
    input_by_label = {
        str(asset.get("label") or "").strip(): asset
        for asset in compact_payload.get("assets") or []
        if str(asset.get("label") or "").strip()
    }
    for asset in review.assets:
        asset_input = input_by_label.get(asset.label) or {}
        source_part = str(asset_input.get("asset_part") or compact_payload.get("source_part") or "")
        if asset.action in {"add_placeholder", "move_placeholder"}:
            if asset.target_field == "none":
                raise ValidationError(f"Step4 asset {asset.label} cannot {asset.action} into target_field=none")
            allowed_target_fields = _allowed_target_fields_for_source_part(source_part)
            if allowed_target_fields and asset.target_field not in allowed_target_fields:
                raise ValidationError(
                    f"Step4 asset {asset.label} from source_part={source_part} cannot target {asset.target_field}"
                )
            if asset.target_field == "options_latex" and not asset.option_label:
                raise ValidationError(
                    f"Step4 asset {asset.label} targets options_latex but option_label is empty"
                )
        if asset.action in {"remove_placeholder", "move_placeholder"}:
            if asset.source_field == "options_latex" and not asset.option_label:
                raise ValidationError(
                    f"Step4 asset {asset.label} removes from options_latex but option_label is empty"
                )


def _allowed_target_fields_for_source_part(source_part: str) -> set[str]:
    if source_part == "paper":
        return {"stem_latex", "options_latex"}
    if source_part == "answer":
        return {"answer_latex", "analysis_latex"}
    if source_part == "mixed":
        return {"stem_latex", "options_latex", "answer_latex", "analysis_latex"}
    return set()


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


def _filter_unmapped_visual_assets(review: VisualAssetReview, compact_payload: dict[str, Any]) -> VisualAssetReview:
    valid_labels = {
        str(asset.get("label") or "").strip()
        for asset in compact_payload.get("assets") or []
        if str(asset.get("label") or "").strip()
    }
    if not valid_labels:
        return VisualAssetReview.from_dict(
            {
                "assets": [],
                "risks": [
                    item.to_dict() for item in review.risks
                ],
            }
        )
    valid_assets = []
    risks = [item.to_dict() for item in review.risks]
    for asset in review.assets:
        if asset.label in valid_labels:
            placeholder_label = _placeholder_src_label(asset.placeholder)
            if placeholder_label and placeholder_label != asset.label:
                risks.append(
                    VisualAssetRisk(
                        label=asset.label,
                        severity="warning",
                        reason=f"模型返回的 placeholder src={placeholder_label} 与资产标签不一致，已阻止同步到 question bank。",
                    ).to_dict()
                )
                continue
            valid_assets.append(asset.to_dict())
            continue
        risks.append(
            VisualAssetRisk(
                label=asset.label,
                severity="warning",
                reason="模型返回了 Step4 compact input 中不存在的资产标签，已阻止同步到 question bank。",
            ).to_dict()
        )
    return VisualAssetReview.from_dict({"assets": valid_assets, "risks": risks})


def _apply_deterministic_visual_asset_overrides(
    review: VisualAssetReview,
    compact_payload: dict[str, Any],
) -> VisualAssetReview:
    overrides: dict[str, dict[str, Any]] = {}
    risks = [item.to_dict() for item in review.risks]
    for asset_input in compact_payload.get("assets") or []:
        if not _is_low_signal_noise_asset(asset_input):
            continue
        label = str(asset_input.get("label") or "").strip()
        if not label:
            continue
        overrides[label] = _low_signal_noise_assignment(asset_input)
        quality = asset_input.get("source_image_quality") or {}
        risks.append(
            VisualAssetRisk(
                label=label,
                severity="info",
                reason=(
                    "源图像接近空白且低对比，已按透印或扫描噪声覆盖为不渲染资产；"
                    f"mean_luma={quality.get('mean_luma')}, "
                    f"std_luma={quality.get('std_luma')}, "
                    f"dark_pixel_ratio={quality.get('dark_pixel_ratio')}。"
                ),
            ).to_dict()
        )
    if not overrides:
        return review

    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in review.assets:
        override = overrides.get(asset.label)
        if override:
            assets.append(override)
            seen.add(asset.label)
        else:
            assets.append(asset.to_dict())
    for label, override in overrides.items():
        if label not in seen:
            assets.append(override)
    return VisualAssetReview.from_dict({"assets": assets, "risks": risks})


def _normalize_unspecified_option_targets(
    review: VisualAssetReview,
    compact_payload: dict[str, Any],
) -> VisualAssetReview:
    input_by_label = {
        str(asset.get("label") or "").strip(): asset
        for asset in compact_payload.get("assets") or []
        if str(asset.get("label") or "").strip()
    }
    assets: list[dict[str, Any]] = []
    risks = [item.to_dict() for item in review.risks]
    for asset in review.assets:
        normalized = _normalize_unspecified_option_target(asset.to_dict(), input_by_label.get(asset.label) or {})
        if normalized is not None:
            assets.append(normalized)
            risks.append(
                VisualAssetRisk(
                    label=asset.label,
                    severity="info",
                    reason="模型输出 options_latex 但未指定 option_label；Step3 已有非选项字段占位，已保留原字段以避免误移入选项。",
                ).to_dict()
            )
        else:
            assets.append(asset.to_dict())
    return VisualAssetReview.from_dict({"assets": assets, "risks": risks})


def _normalize_unspecified_option_target(
    asset: dict[str, Any],
    asset_input: dict[str, Any],
) -> dict[str, Any] | None:
    if asset.get("action") not in {"add_placeholder", "move_placeholder"}:
        return None
    if asset.get("target_field") != "options_latex" or asset.get("option_label"):
        return None
    ref = _first_non_option_placeholder_ref(asset_input.get("step3_placeholder_refs") or [])
    if not ref:
        return None
    placeholder = str(ref.get("placeholder") or asset.get("placeholder") or "")
    field = str(ref.get("field") or "")
    if not placeholder or field not in {"stem_latex", "answer_latex", "analysis_latex"}:
        return None
    normalized = dict(asset)
    normalized.update(
        {
            "question_no": int(ref.get("question_no") or asset.get("question_no") or 0),
            "placeholder_status": "matched",
            "action": "keep_existing",
            "source_field": field,
            "target_field": field,
            "insert_position": "existing_position",
            "asset_tag": _placeholder_tag(placeholder) or str(asset.get("asset_tag") or "img"),
            "placeholder": placeholder,
            "option_group_no": "",
            "option_label": "",
            "reason": (
                str(asset.get("reason") or "")
                + " 本地规则：未指定具体选项时不得移入 options_latex，保留 Step3 已有非选项字段占位。"
            ).strip(),
        }
    )
    return normalized


def _first_non_option_placeholder_ref(refs: list[Any]) -> dict[str, Any]:
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        field = str(ref.get("field") or "")
        if field in {"stem_latex", "answer_latex", "analysis_latex"}:
            return ref
    return {}


def _placeholder_tag(placeholder: str) -> str:
    match = PLACEHOLDER_RE.fullmatch((placeholder or "").strip())
    if not match:
        return ""
    return match.group(1)


def _is_low_signal_noise_asset(asset_input: dict[str, Any]) -> bool:
    quality = asset_input.get("source_image_quality") or {}
    return bool(quality.get("low_signal_noise_candidate"))


def _low_signal_noise_assignment(asset_input: dict[str, Any]) -> dict[str, Any]:
    label = str(asset_input.get("label") or "").strip()
    refs = asset_input.get("step3_placeholder_refs") or []
    first_ref = refs[0] if refs else {}
    candidate_qnos = asset_input.get("candidate_qnos") or []
    question_no = int(first_ref.get("question_no") or _first_int(candidate_qnos) or 0)
    placeholder = str(first_ref.get("placeholder") or "")
    quality = asset_input.get("source_image_quality") or {}
    if placeholder:
        placeholder_status = "placeholder_but_not_belong"
        action = "remove_placeholder"
        insert_position = "remove_existing"
    else:
        placeholder_status = "no_placeholder_needed"
        action = "ignore_asset"
        insert_position = "none"
    return {
        "label": label,
        "question_no": question_no,
        "placeholder_status": placeholder_status,
        "action": action,
        "source_field": "none",
        "target_field": "none",
        "insert_position": insert_position,
        "asset_tag": "none",
        "placeholder": placeholder,
        "option_group_no": "",
        "option_label": "",
        "caption_labels": [],
        "caption_text": "",
        "confidence": 1.0,
        "reason": (
            "本地源图像信号检测判定该资产接近空白且低对比，符合背面透印、反面透回或扫描噪声特征，"
            "不属于当前题的正面可见图片；"
            f"mean_luma={quality.get('mean_luma')}, "
            f"std_luma={quality.get('std_luma')}, "
            f"dark_pixel_ratio={quality.get('dark_pixel_ratio')}。"
        ),
    }


def _first_int(values: list[Any]) -> int:
    for value in values:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _placeholder_src_label(placeholder: str) -> str:
    match = PLACEHOLDER_RE.fullmatch((placeholder or "").strip())
    if not match:
        return ""
    return match.group(2)


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
    source_block_cache: dict[str, dict[str, dict[str, Any]]] = {}
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
                source_block_cache=source_block_cache,
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
                source_block_cache=source_block_cache,
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
    source_block_cache: dict[str, dict[str, dict[str, Any]]],
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
        asset_payload = {
            "label": label,
            "kind": kind,
            "candidate_qnos": candidate_qnos,
            "candidate_fields": _candidate_fields_for_source(source_part),
            "asset_part": source_part,
            "step2_relation": relation,
            "step2_visual_label": relation == "visual_label",
            "step3_placeholder_refs": refs_by_label.get(label, []),
        }
        source_quality = _source_image_quality(
            run_context=run_context,
            packet=packet,
            block=block,
            source_part=source_part,
            kind=kind,
            label=label,
            source_block_cache=source_block_cache,
        )
        if source_quality:
            asset_payload["source_image_quality"] = source_quality
        assets.append(asset_payload)
    return {
        "run_id": run_context.run_id,
        "range_mode": range_mode,
        "source_part": source_part,
        "page": page,
        "step2_question_ranges": step2_question_ranges,
        "step3_records": step3_records,
        "assets": assets,
    }


def _source_image_quality(
    *,
    run_context: RunContext,
    packet: dict[str, Any],
    block: dict[str, Any],
    source_part: str,
    kind: str,
    label: str,
    source_block_cache: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    if kind not in {"image", "chart"}:
        return {}
    part_dir = run_context.source_run_dir / source_part
    if not part_dir.exists():
        return {}
    page = int(packet.get("page") or block.get("page") or 0)
    ref = SourceAssetRef(
        label=label,
        kind=kind,
        source_part=source_part,
        block_id=str(block.get("block_id") or ""),
        figure_id=str(block.get("figure_id") or ""),
        page=page,
        packet_text=str(block.get("text") or ""),
        packet_path=str(block.get("path") or ""),
    )
    source_block = _resolve_source_block(
        part_dir=part_dir,
        block_id=ref.block_id,
        figure_id=ref.figure_id,
        cache=source_block_cache,
    )
    if source_block is None:
        return {}
    source_image = _resolve_source_image_path(part_dir, source_block, ref)
    if source_image is None:
        return {}
    return _measure_image_signal(source_image)


def _measure_image_signal(image_path: Path) -> dict[str, Any]:
    try:
        from PIL import Image, ImageStat
    except ImportError as exc:
        raise RuntimeError("Pillow is required for Step4 source image signal checks") from exc

    with Image.open(image_path) as image:
        gray = image.convert("L")
        width, height = gray.size
        pixel_count = max(1, width * height)
        histogram = gray.histogram()
        stat = ImageStat.Stat(gray)
        mean_luma = float(stat.mean[0])
        std_luma = float(stat.stddev[0])
        dark_pixel_ratio = sum(histogram[:200]) / pixel_count
        very_dark_pixel_ratio = sum(histogram[:120]) / pixel_count
        nonwhite_pixel_ratio = sum(histogram[:245]) / pixel_count
    low_signal = (
        mean_luma >= LOW_SIGNAL_MIN_MEAN_LUMA
        and std_luma <= LOW_SIGNAL_MAX_STD_LUMA
        and dark_pixel_ratio <= LOW_SIGNAL_MAX_DARK_PIXEL_RATIO
    )
    return {
        "width": width,
        "height": height,
        "mean_luma": round(mean_luma, 3),
        "std_luma": round(std_luma, 3),
        "dark_pixel_ratio": round(dark_pixel_ratio, 6),
        "very_dark_pixel_ratio": round(very_dark_pixel_ratio, 6),
        "nonwhite_pixel_ratio": round(nonwhite_pixel_ratio, 6),
        "low_signal_noise_candidate": low_signal,
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
        return ["stem_latex", "options_latex"]
    if source_part == "answer":
        return ["answer_latex", "analysis_latex"]
    return ["stem_latex", "options_latex", "answer_latex", "analysis_latex"]


def _placeholder_refs_by_label(records: list[QuestionRecord]) -> dict[str, list[dict[str, Any]]]:
    refs: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for field_name, segments in (
            ("stem_latex", record.stem_latex),
            ("answer_latex", record.answer_latex),
            ("analysis_latex", record.analysis_latex),
        ):
            for segment in segments:
                _collect_refs(refs, record.question_no, field_name, "", "", segment)
        for group in record.options_latex:
            for option in group.options:
                for segment in option.content_latex:
                    _collect_refs(
                        refs,
                        record.question_no,
                        "options_latex",
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
