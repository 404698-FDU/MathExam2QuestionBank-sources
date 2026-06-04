from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


INPUT_MODES = {
    "pure_paper",
    "paper_plus_answer_file",
    "mixed",
    "answer_patch_for_existing_pure_paper",
}
MODE_ALIASES = {
    "answer_patch": "answer_patch_for_existing_pure_paper",
    "paper_answer": "paper_plus_answer_file",
}
PAGE_RANGE_RE = re.compile(r"^\s*\d+\s*(?:-\s*\d+\s*)?(?:,\s*\d+\s*(?:-\s*\d+\s*)?)*$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"spec must be a JSON object: {path}")
    return data


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def nonempty_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_mode(value: str) -> str:
    mode = MODE_ALIASES.get(nonempty_text(value), nonempty_text(value))
    if mode not in INPUT_MODES:
        raise ValueError(f"input_mode must be one of {sorted(INPUT_MODES)}, got: {value!r}")
    return mode


def discover_v11_root(start: Path) -> Path | None:
    embedded = Path(__file__).resolve().parents[1] / "assets" / "v11_runtime"
    if (embedded / "run_pipeline.py").exists():
        return embedded.resolve()
    candidates = [start, *start.parents]
    for base in candidates:
        direct = base
        nested = base / "code" / "worktrees" / "v11_worktree"
        for candidate in (direct, nested):
            if (candidate / "run_pipeline.py").exists():
                return candidate.resolve()
    return None


def require_local_file(label: str, value: str, errors: list[str]) -> None:
    if not value:
        errors.append(f"missing required source: {label}")
        return
    if is_url(value):
        errors.append(f"{label} is a URL; generic v11 PDF import requires a local file: {value}")
        return
    if not Path(value).expanduser().exists():
        errors.append(f"{label} does not exist: {value}")


def validate_page_range(label: str, value: str, errors: list[str]) -> None:
    if value and not PAGE_RANGE_RE.match(value):
        errors.append(f"{label} has invalid page range syntax: {value!r}")


def active_worker_models(llm: dict[str, Any]) -> list[str]:
    primary = nonempty_text(llm.get("primary_model"))
    workers = [nonempty_text(item) for item in llm.get("worker_models") or [] if nonempty_text(item)]
    limits = llm.get("model_tpm_limits") or {}
    if not workers and isinstance(limits, dict) and len(limits) > 1:
        workers = [str(model) for model in limits]
    return list(dict.fromkeys(workers or ([primary] if primary else [])))


def validate_model_config(llm: dict[str, Any], errors: list[str]) -> None:
    primary = nonempty_text(llm.get("primary_model"))
    if not primary:
        errors.append("llm.primary_model is required")
    provider = nonempty_text(llm.get("provider") or "siliconflow")
    if provider not in {"siliconflow", "bailian", "env"}:
        errors.append(f"llm.provider must be siliconflow, bailian, or env, got: {provider}")
    image_mode = nonempty_text(llm.get("image_token_mode") or "auto")
    if image_mode not in {"auto", "processor", "formula"}:
        errors.append(f"llm.image_token_mode must be auto, processor, or formula, got: {image_mode}")
    limits = llm.get("model_tpm_limits") or {}
    if limits and not isinstance(limits, dict):
        errors.append("llm.model_tpm_limits must be an object mapping model to TPM limit")
        return
    if limits:
        for model, limit in limits.items():
            try:
                limit_int = int(limit)
            except (TypeError, ValueError):
                errors.append(f"TPM limit for {model!r} is not an integer: {limit!r}")
                continue
            if limit_int <= 0:
                errors.append(f"TPM limit for {model!r} must be positive")
        if primary and primary not in limits:
            errors.append(f"llm.model_tpm_limits is missing primary model: {primary}")
        missing = [model for model in active_worker_models(llm) if model not in limits]
        if missing:
            errors.append(f"llm.model_tpm_limits is missing worker model(s): {missing}")


def normalize_spec(raw: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    spec = dict(raw)

    run_id = nonempty_text(spec.get("run_id"))
    if not run_id:
        errors.append("run_id is required")
    elif not RUN_ID_RE.match(run_id):
        errors.append(f"run_id must be ASCII letters/digits/_.- only: {run_id!r}")

    try:
        mode = normalize_mode(str(spec.get("input_mode") or ""))
    except ValueError as exc:
        errors.append(str(exc))
        mode = ""

    v11_root_text = nonempty_text(args.v11_root or spec.get("v11_root"))
    v11_root = Path(v11_root_text).resolve() if v11_root_text else discover_v11_root(Path.cwd())
    if v11_root is None:
        errors.append("v11_root is required and could not be discovered")
        v11_root = Path(".").resolve()
    elif not (v11_root / "run_pipeline.py").exists():
        errors.append(f"v11_root has no run_pipeline.py: {v11_root}")

    runs_root_text = nonempty_text(args.runs_root or spec.get("runs_root"))
    runs_root = Path(runs_root_text).resolve() if runs_root_text else (v11_root / "runs").resolve()

    sources = spec.get("sources") if isinstance(spec.get("sources"), dict) else {}
    source_rules = spec.get("source_rules") if isinstance(spec.get("source_rules"), dict) else {}
    llm = spec.get("llm") if isinstance(spec.get("llm"), dict) else {}
    mineru = spec.get("mineru") if isinstance(spec.get("mineru"), dict) else {}
    cache_policy = spec.get("cache_policy") if isinstance(spec.get("cache_policy"), dict) else {}
    steps = spec.get("steps") if isinstance(spec.get("steps"), dict) else {}

    for label in ("paper_page_range", "answer_page_range"):
        validate_page_range(f"sources.{label}", nonempty_text(sources.get(label)), errors)

    if mode == "pure_paper":
        require_local_file("sources.paper_pdf", nonempty_text(sources.get("paper_pdf")), errors)
    elif mode == "paper_plus_answer_file":
        require_local_file("sources.paper_pdf", nonempty_text(sources.get("paper_pdf")), errors)
        require_local_file("sources.answer_pdf", nonempty_text(sources.get("answer_pdf")), errors)
    elif mode == "mixed":
        mixed_pdf = nonempty_text(sources.get("mixed_pdf") or sources.get("paper_pdf"))
        require_local_file("sources.mixed_pdf or sources.paper_pdf", mixed_pdf, errors)
    elif mode == "answer_patch_for_existing_pure_paper":
        require_local_file("sources.answer_pdf", nonempty_text(sources.get("answer_pdf")), errors)
        if not (v11_root / "run_answer_patch.py").exists():
            errors.append(f"run_answer_patch.py is missing under v11_root: {v11_root}")
        source_run = runs_root / "source_runs" / run_id / "paper" / "ocr_blocks.json"
        step2_range = runs_root / "step2_exam_blocks" / f"{run_id}_raw_units" / "step2_pure_paper_ranges.json"
        if run_id and not source_run.exists():
            errors.append(f"answer patch requires existing paper OCR: {source_run}")
        if run_id and not step2_range.exists():
            errors.append(f"answer patch requires existing pure-paper Step2 ranges: {step2_range}")

    validate_model_config(llm, errors)
    token_file = nonempty_text(mineru.get("token_file"))
    if token_file and not Path(token_file).expanduser().exists():
        warnings.append(f"mineru.token_file does not exist; env token may still work: {token_file}")
    if not token_file:
        warnings.append("mineru.token_file is empty; v11 defaults or environment must provide MinerU credentials")
    if cache_policy.get("reuse_existing_ocr") and cache_policy.get("force_source"):
        errors.append("cache_policy.reuse_existing_ocr and cache_policy.force_source cannot both be true")

    normalized = {
        **spec,
        "run_id": run_id,
        "input_mode": mode,
        "v11_root": str(v11_root),
        "runs_root": str(runs_root),
        "sources": sources,
        "source_rules": source_rules,
        "llm": {
            "provider": nonempty_text(llm.get("provider") or "siliconflow"),
            "primary_model": nonempty_text(llm.get("primary_model")),
            "worker_models": active_worker_models(llm),
            "model_tpm_limits": llm.get("model_tpm_limits") or {},
            "tpm_limit": int(llm.get("tpm_limit") or 0),
            "token_estimator_model": nonempty_text(llm.get("token_estimator_model")),
            "tpm_output_reserve": int(llm.get("tpm_output_reserve") or 2048),
            "token_budget_pool": nonempty_text(llm.get("token_budget_pool")),
            "token_budget_log": nonempty_text(llm.get("token_budget_log")),
            "token_budget_verbose": bool(llm.get("token_budget_verbose")),
            "image_token_mode": nonempty_text(llm.get("image_token_mode") or "auto"),
            "timeout": int(llm.get("timeout") or 180),
            "step3_timeout": llm.get("step3_timeout"),
            "step3_5_timeout": llm.get("step3_5_timeout"),
            "asset_timeout": llm.get("asset_timeout"),
            "max_workers": int(llm.get("max_workers") or 8),
            "enable_thinking": bool(llm.get("enable_thinking")),
        },
        "mineru": {
            "token_file": token_file,
            "timeout": int(mineru.get("timeout") or 900),
            "poll_interval": int(mineru.get("poll_interval") or 10),
            "extract_retries": int(mineru.get("extract_retries") or 3),
            "extract_retry_sleep": int(mineru.get("extract_retry_sleep") or 60),
            "dpi": int(mineru.get("dpi") or 144),
        },
        "cache_policy": {
            "reuse_existing_ocr": bool(cache_policy.get("reuse_existing_ocr")),
            "force_source": bool(cache_policy.get("force_source")),
            "force_pipeline": bool(cache_policy.get("force_pipeline", True)),
            "force_answer_ranges": bool(cache_policy.get("force_answer_ranges")),
        },
        "steps": {
            "skip_step2": bool(steps.get("skip_step2")),
            "skip_step3": bool(steps.get("skip_step3")),
            "step3_5": bool(steps.get("step3_5", True)),
            "skip_step4": bool(steps.get("skip_step4")),
            "skip_render": bool(steps.get("skip_render")),
        },
    }
    return normalized, errors, warnings


def add_llm_args(cmd: list[str], spec: dict[str, Any]) -> None:
    llm = spec["llm"]
    cmd.extend(["--model", llm["primary_model"]])
    cmd.extend(["--llm-provider", llm["provider"]])
    cmd.extend(["--timeout", str(llm["timeout"])])
    if llm.get("step3_timeout") is not None:
        cmd.extend(["--step3-timeout", str(llm["step3_timeout"])])
    if llm.get("step3_5_timeout") is not None:
        cmd.extend(["--step3-5-timeout", str(llm["step3_5_timeout"])])
    if llm.get("asset_timeout") is not None:
        cmd.extend(["--asset-timeout", str(llm["asset_timeout"])])
    cmd.extend(["--max-workers", str(llm["max_workers"])])
    cmd.extend(["--tpm-limit", str(llm["tpm_limit"])])
    cmd.extend(["--token-estimator-model", llm["token_estimator_model"] or llm["primary_model"]])
    cmd.extend(["--image-token-mode", llm["image_token_mode"]])
    cmd.extend(["--tpm-output-reserve", str(llm["tpm_output_reserve"])])
    if llm["token_budget_pool"]:
        cmd.extend(["--token-budget-pool", llm["token_budget_pool"]])
    if llm["token_budget_log"]:
        cmd.extend(["--token-budget-log", llm["token_budget_log"]])
    if llm["token_budget_verbose"]:
        cmd.append("--token-budget-verbose")
    for model in llm["worker_models"]:
        cmd.extend(["--worker-model", model])
    for model, limit in (llm.get("model_tpm_limits") or {}).items():
        cmd.extend(["--model-tpm-limit", f"{model}={int(limit)}"])
    cmd.append("--enable-thinking" if llm["enable_thinking"] else "--no-enable-thinking")


def add_mineru_args(cmd: list[str], spec: dict[str, Any]) -> None:
    mineru = spec["mineru"]
    if mineru["token_file"]:
        cmd.extend(["--mineru-token-file", mineru["token_file"]])
    cmd.extend(["--mineru-timeout", str(mineru["timeout"])])
    cmd.extend(["--mineru-poll-interval", str(mineru["poll_interval"])])
    cmd.extend(["--extract-retries", str(mineru["extract_retries"])])
    cmd.extend(["--extract-retry-sleep", str(mineru["extract_retry_sleep"])])
    cmd.extend(["--dpi", str(mineru["dpi"])])


def build_commands(spec: dict[str, Any]) -> list[dict[str, Any]]:
    skill_root = Path(__file__).resolve().parents[1]
    v11_root = Path(spec["v11_root"])
    runs_root = Path(spec["runs_root"])
    mode = spec["input_mode"]
    sources = spec["sources"]
    source_rules = spec["source_rules"]
    cache = spec["cache_policy"]
    steps = spec["steps"]
    commands: list[dict[str, Any]] = []

    if mode == "answer_patch_for_existing_pure_paper":
        cmd = [
            "python.exe",
            str(v11_root / "run_answer_patch.py"),
            "--runs-root",
            str(runs_root),
            "--run-id",
            spec["run_id"],
            "--answer-pdf",
            nonempty_text(sources.get("answer_pdf")),
        ]
        if nonempty_text(sources.get("answer_page_range")):
            cmd.extend(["--answer-page-range", nonempty_text(sources.get("answer_page_range"))])
        if nonempty_text(source_rules.get("answer")):
            cmd.extend(["--answer-source-rule", nonempty_text(source_rules.get("answer"))])
        add_llm_args(cmd, spec)
        add_mineru_args(cmd, spec)
        if cache["force_source"]:
            cmd.append("--force-answer")
        if cache["force_answer_ranges"]:
            cmd.append("--force-answer-ranges")
        if steps["skip_step3"]:
            cmd.append("--skip-step3")
        if not steps["step3_5"]:
            cmd.append("--no-step3-5")
        if steps["skip_step4"]:
            cmd.append("--skip-step4")
        if steps["skip_render"]:
            cmd.append("--skip-render")
        commands.append({"label": "answer_patch", "cmd": cmd})
        return commands

    source_cmd = [
        "python.exe",
        str(skill_root / "scripts" / "v11_source_import.py"),
        "--v11-root",
        str(v11_root),
        "--runs-root",
        str(runs_root),
        "--run-id",
        spec["run_id"],
        "--mode",
        mode,
    ]
    if mode == "mixed":
        source_cmd.extend(["--mixed-pdf", nonempty_text(sources.get("mixed_pdf") or sources.get("paper_pdf"))])
    else:
        source_cmd.extend(["--paper-pdf", nonempty_text(sources.get("paper_pdf"))])
    if nonempty_text(sources.get("paper_page_range")):
        source_cmd.extend(["--paper-page-range", nonempty_text(sources.get("paper_page_range"))])
    if mode == "paper_plus_answer_file":
        source_cmd.extend(["--answer-pdf", nonempty_text(sources.get("answer_pdf"))])
        if nonempty_text(sources.get("answer_page_range")):
            source_cmd.extend(["--answer-page-range", nonempty_text(sources.get("answer_page_range"))])
    if nonempty_text(source_rules.get("paper")):
        source_cmd.extend(["--paper-source-rule", nonempty_text(source_rules.get("paper"))])
    if nonempty_text(source_rules.get("answer")):
        source_cmd.extend(["--answer-source-rule", nonempty_text(source_rules.get("answer"))])
    add_mineru_args(source_cmd, spec)
    if cache["force_source"]:
        source_cmd.append("--force-source")
    if cache["reuse_existing_ocr"]:
        source_cmd.append("--reuse-existing-ocr")
    commands.append({"label": "source_import", "cmd": source_cmd})

    pipeline_cmd = [
        "python.exe",
        str(v11_root / "run_pipeline.py"),
        "--source-runs-root",
        str(runs_root / "source_runs"),
        "--runs-root",
        str(runs_root / "step2_exam_blocks"),
        "--qb-root",
        str(runs_root / "question_bank"),
        "--render-root",
        str(runs_root / "rendered_question_bank_mathjax"),
        "--step3-5-root",
        str(runs_root / "reviews_step3_5_latex_audit"),
        "--run-id",
        spec["run_id"],
        "--mode",
        mode,
    ]
    add_llm_args(pipeline_cmd, spec)
    if cache["force_pipeline"]:
        pipeline_cmd.append("--force")
    if steps["skip_step2"]:
        pipeline_cmd.append("--skip-step2")
    if steps["skip_step3"]:
        pipeline_cmd.append("--skip-step3")
    if not steps["step3_5"]:
        pipeline_cmd.append("--no-step3-5")
    if steps["skip_step4"]:
        pipeline_cmd.append("--skip-step4")
    if steps["skip_render"]:
        pipeline_cmd.append("--skip-render")
    commands.append({"label": "pipeline", "cmd": pipeline_cmd})
    return commands


def format_command(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate an exam-import spec and emit v11 commands.")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--v11-root", type=Path, default=None)
    parser.add_argument("--runs-root", type=Path, default=None)
    parser.add_argument("--write-normalized", type=Path, default=None)
    parser.add_argument("--emit", choices=["json", "powershell"], default="json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    raw = load_json(args.spec)
    spec, errors, warnings = normalize_spec(raw, args)
    commands = [] if errors else build_commands(spec)
    payload = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized_spec": spec,
        "commands": commands,
        "powershell": [{"label": item["label"], "command": format_command(item["cmd"])} for item in commands],
    }
    if args.write_normalized and not errors:
        write_json(args.write_normalized, spec)
    if args.emit == "powershell":
        for item in payload["powershell"]:
            print(f"# {item['label']}")
            print(item["command"])
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
