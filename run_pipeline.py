from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKTREE_ROOT = Path(__file__).resolve().parent
CODE_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import step2_layout  # noqa: E402
import step3_question2json  # noqa: E402
import step4_asset_distribution  # noqa: E402
from common_io import read_json, write_json, write_jsonl  # noqa: E402
from common_llm import configure_llm_provider_env  # noqa: E402
from common_token_budget import DEFAULT_OUTPUT_RESERVE_TOKENS, configure_token_budget_env  # noqa: E402


DEFAULT_MODEL = "Qwen/Qwen3.6-35B-A3B"
DEFAULT_RUNS_ROOT = WORKTREE_ROOT / "runs_step2_exam_blocks"
DEFAULT_QB_ROOT = WORKTREE_ROOT / "runs_question_bank"
DEFAULT_RENDER_ROOT = WORKTREE_ROOT / "rendered_question_bank_mathjax"
DEFAULT_STEP3_5_ROOT = WORKTREE_ROOT / "reviews_step3_5_latex_audit"

def run_subprocess(cmd: list[str], env: dict[str, str] | None = None) -> None:
    print(json.dumps({"event": "subprocess", "cmd": cmd}, ensure_ascii=False), flush=True)
    subprocess.run(cmd, cwd=str(WORKTREE_ROOT), env=env, check=True)


def run_parallel_subprocesses(commands: list[dict[str, Any]], env: dict[str, str]) -> None:
    processes: list[tuple[str, subprocess.Popen[Any]]] = []
    for item in commands:
        label = str(item["label"])
        cmd = list(item["cmd"])
        print(json.dumps({"event": "subprocess_start", "label": label, "cmd": cmd}, ensure_ascii=False), flush=True)
        processes.append((label, subprocess.Popen(cmd, cwd=str(WORKTREE_ROOT), env=env)))
    failures: list[dict[str, Any]] = []
    for label, process in processes:
        returncode = process.wait()
        print(
            json.dumps(
                {"event": "subprocess_done", "label": label, "returncode": returncode},
                ensure_ascii=False,
            ),
            flush=True,
        )
        if returncode != 0:
            failures.append({"label": label, "returncode": returncode})
    if failures:
        raise RuntimeError(f"Parallel subprocess failed: {failures}")


def safe_model_slug(model: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("._-")
    return slug or "model"


def ensure_removable_child(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    root = parent.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Refusing to remove path outside {root}: {resolved}") from exc


def remove_tree_if_exists(path: Path, parent: Path) -> None:
    if not path.exists():
        return
    ensure_removable_child(path, parent)
    shutil.rmtree(path)


def distribute_workers(total_workers: int, active_count: int) -> list[int]:
    if active_count <= 0:
        return []
    total = max(1, int(total_workers))
    base = max(1, total // active_count)
    extra = max(0, total - base * active_count)
    return [base + (1 if index < extra else 0) for index in range(active_count)]


def assign_questions_to_models(run_id: str, runs_root: Path, worker_models: list[str]) -> dict[str, dict[str, Any]]:
    if not worker_models:
        raise ValueError("worker_models must not be empty")
    payloads = step3_question2json.build_payloads_for_run(run_id, runs_root)
    if not payloads:
        raise RuntimeError(f"No Step3 payloads found for {run_id}")
    items = [
        {
            "question_no": int(payload["question_no"]),
            "weight": int(step3_question2json.estimate_task_weight(payload)),
        }
        for payload in payloads
    ]
    assignments = {
        model: {"question_numbers": [], "weight": 0, "items": []}
        for model in worker_models
    }
    model_order = {model: index for index, model in enumerate(worker_models)}
    for item in sorted(items, key=lambda row: (-int(row["weight"]), int(row["question_no"]))):
        model = min(
            worker_models,
            key=lambda name: (
                int(assignments[name]["weight"]),
                len(assignments[name]["question_numbers"]),
                model_order[name],
            ),
        )
        assignments[model]["question_numbers"].append(int(item["question_no"]))
        assignments[model]["items"].append(item)
        assignments[model]["weight"] = int(assignments[model]["weight"]) + int(item["weight"])
    for model in worker_models:
        assignments[model]["question_numbers"].sort()
        assignments[model]["items"].sort(key=lambda row: int(row["question_no"]))
    return assignments


def copy_directory_contents(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def merge_step3_worker_outputs(
    run_id: str,
    qb_root: Path,
    worker_output_roots: dict[str, Path],
    assignments: dict[str, dict[str, Any]],
    worker_models: list[str],
    force: bool,
) -> dict[str, Any]:
    out_dir = qb_root / run_id
    if force:
        remove_tree_if_exists(out_dir, qb_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    per_question_dir = out_dir / "per_question"
    records_by_qno: dict[int, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    partitions: list[dict[str, Any]] = []
    queue_items: list[dict[str, Any]] = []

    for model in worker_models:
        question_numbers = [int(qno) for qno in assignments.get(model, {}).get("question_numbers") or []]
        if not question_numbers:
            continue
        worker_run_dir = worker_output_roots[model] / run_id
        records = read_json(worker_run_dir / "question_bank.json", [])
        if not isinstance(records, list):
            raise RuntimeError(f"Invalid Step3 worker question_bank.json: {worker_run_dir}")
        summary = read_json(worker_run_dir / "summary.json", {})
        for record in records:
            qno = int(record.get("question_no") or 0)
            if qno in records_by_qno:
                raise RuntimeError(f"Duplicate Step3 record for {run_id} Q{qno}")
            records_by_qno[qno] = record
        for result in summary.get("results") or []:
            if isinstance(result, dict):
                row = dict(result)
                row["worker_model"] = model
                results.append(row)
        copy_directory_contents(worker_run_dir / "per_question", per_question_dir)
        worker_queue = read_json(worker_run_dir / "queue_plan.json", {})
        for item in worker_queue.get("items") or []:
            if isinstance(item, dict):
                row = dict(item)
                row["worker_model"] = model
                queue_items.append(row)
        partitions.append(
            {
                "model": model,
                "question_numbers": question_numbers,
                "question_count": len(question_numbers),
                "weight": int(assignments.get(model, {}).get("weight") or 0),
                "output_dir": str(worker_run_dir),
                "status_counts": summary.get("status_counts") if isinstance(summary, dict) else None,
            }
        )

    expected_qnos = sorted(
        {
            int(qno)
            for data in assignments.values()
            for qno in (data.get("question_numbers") or [])
        }
    )
    actual_qnos = sorted(records_by_qno)
    if actual_qnos != expected_qnos:
        raise RuntimeError(f"Step3 merge mismatch for {run_id}: expected={expected_qnos}, actual={actual_qnos}")

    records = [records_by_qno[qno] for qno in actual_qnos]
    model_summary = "multi_model[" + ",".join(worker_models) + "]"
    summary = step3_question2json.summarize(run_id, records, results, out_dir, model_summary)
    summary.update(
        {
            "model": model_summary,
            "worker_models": worker_models,
            "worker_partitions": partitions,
            "worker_output_roots": {model: str(path) for model, path in worker_output_roots.items()},
        }
    )
    write_json(out_dir / "summary.json", summary)
    write_json(out_dir / "question_bank.json", records)
    write_jsonl(out_dir / "question_bank.jsonl", records)
    write_json(
        out_dir / "queue_plan.json",
        {
            "strategy": "multi_model_greedy_longest_question_answer_first",
            "worker_models": worker_models,
            "partitions": partitions,
            "items": sorted(queue_items, key=lambda row: int(row.get("question_no") or 0)),
        },
    )
    return summary


def run_step3(
    run_ids: list[str],
    runs_root: Path,
    qb_root: Path,
    model: str,
    timeout: int,
    max_workers: int,
    force: bool,
    enable_thinking: bool,
    env: dict[str, str],
) -> None:
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "step3_question2json.py"),
        "--runs-root",
        str(runs_root),
        "--output-root",
        str(qb_root),
        "--model",
        model,
        "--timeout",
        str(timeout),
        "--max-workers",
        str(max_workers),
    ]
    for run_id in run_ids:
        cmd.extend(["--run-id", run_id])
    if force:
        cmd.append("--force")
    if enable_thinking:
        cmd.append("--enable-thinking")
    else:
        cmd.append("--no-enable-thinking")
    run_subprocess(cmd, env=env)


def run_step3_multi_model(
    run_ids: list[str],
    runs_root: Path,
    qb_root: Path,
    worker_models: list[str],
    timeout: int,
    max_workers: int,
    force: bool,
    enable_thinking: bool,
    env: dict[str, str],
) -> None:
    worker_base = qb_root / "_step3_worker_outputs"
    for run_id in run_ids:
        assignments = assign_questions_to_models(run_id, runs_root, worker_models)
        active_models = [model for model in worker_models if assignments[model]["question_numbers"]]
        worker_counts = distribute_workers(max_workers, len(active_models))
        worker_output_roots: dict[str, Path] = {}
        commands: list[dict[str, Any]] = []
        for model, model_workers in zip(active_models, worker_counts):
            output_root = worker_base / run_id / safe_model_slug(model)
            if force:
                remove_tree_if_exists(output_root, worker_base)
            worker_output_roots[model] = output_root
            cmd = [
                sys.executable,
                str(SCRIPT_DIR / "step3_question2json.py"),
                "--runs-root",
                str(runs_root),
                "--output-root",
                str(output_root),
                "--model",
                model,
                "--timeout",
                str(timeout),
                "--max-workers",
                str(max(1, min(int(model_workers), len(assignments[model]["question_numbers"])))),
                "--run-id",
                run_id,
            ]
            for qno in assignments[model]["question_numbers"]:
                cmd.extend(["--question-no", str(qno)])
            if force:
                cmd.append("--force")
            if enable_thinking:
                cmd.append("--enable-thinking")
            else:
                cmd.append("--no-enable-thinking")
            commands.append({"label": f"step3:{run_id}:{model}", "cmd": cmd})
        print(
            json.dumps(
                {
                    "event": "step3_multi_model_partition",
                    "run_id": run_id,
                    "worker_models": active_models,
                    "partitions": [
                        {
                            "model": model,
                            "question_numbers": assignments[model]["question_numbers"],
                            "weight": assignments[model]["weight"],
                        }
                        for model in active_models
                    ],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        run_parallel_subprocesses(commands, env=env)
        summary = merge_step3_worker_outputs(
            run_id=run_id,
            qb_root=qb_root,
            worker_output_roots=worker_output_roots,
            assignments=assignments,
            worker_models=active_models,
            force=force,
        )
        print(
            json.dumps(
                {
                    "event": "summary_step3_multi_model",
                    "run_id": run_id,
                    "question_count": summary.get("question_count"),
                    "worker_models": active_models,
                    "status_counts": summary.get("status_counts"),
                    "summary_path": str(qb_root / run_id / "summary.json"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def run_step3_5(
    run_ids: list[str],
    qb_root: Path,
    output_root: Path,
    model: str,
    worker_models: list[str],
    timeout: int,
    max_workers: int,
    enable_thinking: bool,
    env: dict[str, str],
) -> None:
    for run_id in run_ids:
        cmd = [
            sys.executable,
            str(SCRIPT_DIR / "step3_5_question_json_audit_fix.py"),
            "--run-id",
            run_id,
            "--qb-root",
            str(qb_root),
            "--output-root",
            str(output_root),
            "--source-stage",
            "question_bank",
            "--fields",
            "question_surface",
            "--model",
            model,
            "--timeout",
            str(timeout),
            "--max-workers",
            str(max_workers),
            "--write-back",
        ]
        for worker_model in worker_models:
            cmd.extend(["--worker-model", worker_model])
        if enable_thinking:
            cmd.append("--enable-thinking")
        run_subprocess(cmd, env=env)


def render(
    run_ids: list[str],
    qb_root: Path,
    runs_root: Path,
    source_runs_root: Path,
    render_root: Path,
) -> None:
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "step5_vlm_html_mathjax_render.py"),
        "--qb-root",
        str(qb_root),
        "--asset-root",
        str(render_root),
        "--block-root",
        str(runs_root),
        "--source-runs",
        str(source_runs_root),
        "--output-root",
        str(render_root),
    ]
    for run_id in run_ids:
        cmd.extend(["--run-id", run_id])
    run_subprocess(cmd)


def parse_model_tpm_limits(items: list[str] | None) -> dict[str, int]:
    limits: dict[str, int] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--model-tpm-limit must be MODEL=LIMIT, got: {item}")
        model, limit_text = item.rsplit("=", 1)
        model = model.strip()
        if not model:
            raise ValueError(f"--model-tpm-limit has empty model: {item}")
        try:
            limit = int(limit_text)
        except ValueError as exc:
            raise ValueError(f"--model-tpm-limit has non-integer limit: {item}") from exc
        if limit <= 0:
            raise ValueError(f"--model-tpm-limit must be positive: {item}")
        limits[model] = limit
    return limits


def main() -> int:
    parser = argparse.ArgumentParser(description="V11 OCR-to-question-bank pipeline using Step3 JSON Schema.")
    parser.add_argument("--source-runs-root", type=Path, default=WORKTREE_ROOT / "source_runs")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--qb-root", type=Path, default=DEFAULT_QB_ROOT)
    parser.add_argument("--render-root", type=Path, default=DEFAULT_RENDER_ROOT)
    parser.add_argument("--step3-5-root", type=Path, default=DEFAULT_STEP3_5_ROOT)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--worker-model",
        action="append",
        default=[],
        help="Step3 request model. Repeatable; multiple values split questions across models.",
    )
    parser.add_argument("--llm-provider", choices=["siliconflow", "bailian", "env"], default="siliconflow")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--step3-timeout", type=int, default=None)
    parser.add_argument("--step3-5-timeout", type=int, default=None)
    parser.add_argument("--asset-timeout", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--tpm-limit", type=int, default=36_000, help="Local LLM tokens-per-minute limit. Use 0 to disable.")
    parser.add_argument("--token-estimator-model", default=None, help="Tokenizer/processor model. Defaults to --model.")
    parser.add_argument("--image-token-mode", choices=["auto", "processor", "formula"], default="auto")
    parser.add_argument("--tpm-output-reserve", type=int, default=DEFAULT_OUTPUT_RESERVE_TOKENS)
    parser.add_argument("--token-budget-log", type=Path, default=None)
    parser.add_argument("--token-budget-verbose", action="store_true")
    parser.add_argument(
        "--model-tpm-limit",
        action="append",
        default=[],
        help="Per-request-model TPM bucket as MODEL=LIMIT. Repeatable; unknown request models fail.",
    )
    parser.add_argument(
        "--token-budget-pool",
        type=Path,
        default=None,
        help="SQLite file for cross-process TPM sharing. Defaults to runs-root/token_budget_pool.sqlite when model buckets are set.",
    )
    parser.add_argument("--mode", choices=["auto", "pure_paper", "paper_plus_answer_file", "mixed"], default="auto")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-step2", action="store_true")
    parser.add_argument("--skip-step3", action="store_true")
    parser.add_argument("--step3-5", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-step4", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args()

    env = os.environ.copy()
    configure_llm_provider_env(env, args.llm_provider)
    configure_llm_provider_env(os.environ, args.llm_provider)
    token_log_path = (args.token_budget_log or (args.runs_root / "token_budget_events.jsonl")).resolve()
    model_tpm_limits = parse_model_tpm_limits(args.model_tpm_limit)
    worker_models = list(dict.fromkeys(args.worker_model or []))
    if not worker_models and len(model_tpm_limits) > 1:
        worker_models = list(model_tpm_limits)
    if not worker_models:
        worker_models = [args.model]
    if model_tpm_limits:
        if args.model not in model_tpm_limits:
            raise ValueError(f"Missing --model-tpm-limit for primary model: {args.model}")
        missing_worker_limits = [model for model in worker_models if model not in model_tpm_limits]
        if missing_worker_limits:
            raise ValueError(f"Missing --model-tpm-limit for worker model(s): {missing_worker_limits}")
    token_pool_path = (
        args.token_budget_pool
        or (args.runs_root / "token_budget_pool.sqlite" if model_tpm_limits else None)
    )
    if token_pool_path is not None:
        token_pool_path = token_pool_path.resolve()
    token_estimator_model = args.token_estimator_model or DEFAULT_MODEL
    token_budget_enabled = args.tpm_limit > 0 or bool(model_tpm_limits)
    configure_token_budget_env(
        env,
        tpm_limit=args.tpm_limit,
        estimator_model=token_estimator_model,
        image_token_mode=args.image_token_mode,
        output_reserve_tokens=args.tpm_output_reserve,
        verbose=args.token_budget_verbose,
        log_path=token_log_path,
        model_tpm_limits=model_tpm_limits,
        pool_path=token_pool_path,
    )
    configure_token_budget_env(
        os.environ,
        tpm_limit=args.tpm_limit,
        estimator_model=token_estimator_model,
        image_token_mode=args.image_token_mode,
        output_reserve_tokens=args.tpm_output_reserve,
        verbose=args.token_budget_verbose,
        log_path=token_log_path,
        model_tpm_limits=model_tpm_limits,
        pool_path=token_pool_path,
    )
    print(
        json.dumps(
            {
                "event": "token_budget_config",
                "tpm_limit": args.tpm_limit,
                "model_tpm_limits": model_tpm_limits,
                "token_budget_pool": str(token_pool_path) if token_pool_path is not None else None,
                "token_estimator_model": token_estimator_model,
                "image_token_mode": args.image_token_mode,
                "output_reserve_tokens": args.tpm_output_reserve,
                "worker_models": worker_models,
                "log_path": str(token_log_path) if token_budget_enabled else None,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    step3_timeout = args.step3_timeout if args.step3_timeout is not None else args.timeout
    step3_5_timeout = args.step3_5_timeout if args.step3_5_timeout is not None else args.timeout
    asset_timeout = args.asset_timeout if args.asset_timeout is not None else args.timeout
    summaries: list[dict[str, Any]] = []

    if not args.skip_step2:
        for run_id in args.run_id:
            print(f"[v11] {run_id}: Step2 range detection timeout={args.timeout}s model={args.model}", flush=True)
            summary = step2_layout.run_one(
                run_id=run_id,
                source_runs_root=args.source_runs_root,
                output_root=args.runs_root,
                model=args.model,
                timeout=args.timeout,
                force=args.force,
                mode=args.mode,
                worker_models=worker_models,
            )
            summaries.append(summary)
            print(
                f"  mode={summary['mode']} questions={summary['question_count']} "
                f"answered={summary['answered_question_count']} missing_answers={summary['missing_answer_numbers']}",
                flush=True,
            )
    else:
        summaries = [{"run_id": run_id, "step2": "skipped"} for run_id in args.run_id]

    token_budget_summary = {
        "tpm_limit": args.tpm_limit,
        "model_tpm_limits": model_tpm_limits,
        "token_budget_pool": str(token_pool_path) if token_pool_path is not None else None,
        "token_estimator_model": token_estimator_model,
        "image_token_mode": args.image_token_mode,
        "output_reserve_tokens": args.tpm_output_reserve,
        "worker_models": worker_models,
        "log_path": str(token_log_path) if token_budget_enabled else None,
    }
    write_json(args.runs_root / "batch_summary.json", {"runs": summaries, "model": args.model, "token_budget": token_budget_summary})

    if not args.skip_step3:
        if len(worker_models) > 1:
            print(
                f"[v11] Step3 JSON Schema standardization timeout={step3_timeout}s worker_models={worker_models}",
                flush=True,
            )
            run_step3_multi_model(
                run_ids=args.run_id,
                runs_root=args.runs_root,
                qb_root=args.qb_root,
                worker_models=worker_models,
                timeout=step3_timeout,
                max_workers=args.max_workers,
                force=args.force,
                enable_thinking=args.enable_thinking,
                env=env,
            )
        else:
            print(f"[v11] Step3 JSON Schema standardization timeout={step3_timeout}s model={worker_models[0]}", flush=True)
            run_step3(
                run_ids=args.run_id,
                runs_root=args.runs_root,
                qb_root=args.qb_root,
                model=worker_models[0],
                timeout=step3_timeout,
                max_workers=args.max_workers,
                force=args.force,
                enable_thinking=args.enable_thinking,
                env=env,
            )

    if args.step3_5 and not args.skip_step3:
        print(f"[v11] Step3.5 LaTeX audit/fix before Step4 timeout={step3_5_timeout}s worker_models={worker_models}", flush=True)
        run_step3_5(
            run_ids=args.run_id,
            qb_root=args.qb_root,
            output_root=args.step3_5_root,
            model=args.model,
            worker_models=worker_models,
            timeout=step3_5_timeout,
            max_workers=args.max_workers,
            enable_thinking=args.enable_thinking,
            env=env,
        )

    if not args.skip_step4:
        print(f"[v11] Step4 visual/table assignment timeout={asset_timeout}s worker_models={worker_models}", flush=True)
        summary_by_run = {str(item.get("run_id")): item for item in summaries if isinstance(item, dict)}
        for run_id in args.run_id:
            visual_summary = step4_asset_distribution.assign_visual_assets_for_run(
                run_id=run_id,
                runs_root=args.runs_root,
                qb_root=args.qb_root,
                model=args.model,
                timeout=asset_timeout,
                force=args.force,
                worker_models=worker_models,
            )
            summary = summary_by_run.get(run_id)
            if summary is not None:
                summary.update(
                    {
                        "visual_asset_assignment_elapsed_seconds_sum": visual_summary.get("elapsed_seconds_sum"),
                        "visual_asset_assignment_elapsed_seconds_max": visual_summary.get("elapsed_seconds_max"),
                        "visual_asset_assignment_asset_count": visual_summary.get("asset_count"),
                        "asset_match_risk_count": int(visual_summary.get("risk_count") or 0),
                    }
                )
            print(
                f"  {run_id}: assets={visual_summary.get('asset_count')} assigned={visual_summary.get('assigned_count')} "
                f"risks={visual_summary.get('risk_count')}",
                flush=True,
            )

        print("[v11] Step4.5 sync Step4 assets into question-bank metadata", flush=True)
        for run_id in args.run_id:
            sync_summary = step4_asset_distribution.sync_step4_assets_to_question_bank(
                run_id=run_id,
                runs_root=args.runs_root,
                qb_root=args.qb_root,
            )
            summary = summary_by_run.get(run_id)
            if summary is not None:
                summary["step4_question_bank_sync_changed_question_count"] = sync_summary.get("changed_question_count")
            print(
                f"  {run_id}: status={sync_summary.get('status')} changed={sync_summary.get('changed_question_count')}",
                flush=True,
            )
        write_json(args.runs_root / "batch_summary.json", {"runs": summaries, "model": args.model, "token_budget": token_budget_summary})

    if not args.skip_render:
        print("[v11] MathJax render", flush=True)
        render(args.run_id, args.qb_root, args.runs_root, args.source_runs_root, args.render_root)

    print(
        json.dumps(
            {
                "runs": summaries,
                "runs_root": str(args.runs_root),
                "qb_root": str(args.qb_root),
                "render_root": str(args.render_root),
                "token_budget": token_budget_summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
