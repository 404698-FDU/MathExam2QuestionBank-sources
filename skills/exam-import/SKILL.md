---
name: exam-import
description: Run the v12 math exam import and parsing pipeline. Use when Codex is asked to import, parse, refresh, re-import, patch answers, validate an import spec, or run Step2-Step5 for scanned/math exam PDFs, prepared OCR directories, pure-paper, mixed, paper-plus-answer, answer-patch workflows, provider/model/call-spec configuration, MinerU extraction, and evidence reporting.
---

# Exam Import

## Core Rule

Treat an import as a reproducible v12 run, not an ad hoc fix. First convert the user's request into an explicit `import_spec_v2`; then validate it; then run the v12 source import and pipeline entrypoints; then report real artifacts and counts. Do not hand-edit final question JSON to hide pipeline failures.

## Required Inputs

Before running, make sure these are known or intentionally defaulted:

- `run_id`: stable ASCII identifier.
- `input_mode`: one of `pure_paper`, `mixed`, `paper_plus_answer`, `answer_patch`.
- source inputs: exact PDF paths or prepared source-part directories for paper, answer, or mixed content.
- page ranges: required when one PDF contains multiple parts or only part of a PDF should be imported.
- source rules: short provenance notes for paper, answer, or mixed content.
- v12 LLM config: provider, primary model, step call spec paths, timeout, worker count, thinking flag.
- cache policy: whether existing OCR may be reused, whether source extraction and pipeline outputs are forced.
- expected outcome: expected question count, known missing answers, and review focus.

If a required value affects correctness and cannot be inferred from local files, ask the user before running.

## Workflow

1. Locate the bundled v12 runtime at the repository root containing `exam_import/`, `prompts/`, `tool_schemas/`, `provider_config/`, and `call_specs/`. Do not use v11 unless the user explicitly asks for legacy behavior.
2. Read current v12 code and existing artifacts before assuming behavior. Confirm available modes and CLI arguments from local files.
3. Draft an `import_spec_v2` using `assets/import_spec.template.json`.
4. Validate the spec:

```powershell
python.exe <repo-root>/exam_import/cli/validate_spec.py --spec <spec.json>
```

5. If prompt, schema, provider, model, or call spec files changed, run v12 contract checks before model calls.
6. For new imports, run `source_import.py --spec <spec.json>` first. It creates `runs_root/source_runs/<run_id>` and extracts or copies the required source parts.
7. Run `run_pipeline.py --spec <spec.json>` for Step2-Step5.
8. For `answer_patch`, run `source_import.py --spec <spec.json>` to add the answer source, then `answer_patch.py --spec <spec.json>`.
9. Report evidence: source import summary, Step2 alignment counts, Step3 success/error counts, Step3.5 audit status, Step4 asset/sync counts, rendered entry path, and failed questions.

## Resources

- Read `references/import-spec.md` when deciding what the user must specify.
- Read `references/v12-runtime.md` before running or modifying v12 commands.
- Use `assets/import_spec.template.json` as the editable v12 spec starting point.
- Use `<repo-root>/call_specs/` for per-step model call configs.
- Use `<repo-root>/provider_config/` for provider/model static configs.
- Use `<repo-root>/` as the bundled v12 implementation. It contains source import, Step2-Step5, prompt loading, provider/model config, schemas, rendering, and evidence reporting.
- Read `references/v11-runtime.md` only when the user explicitly asks for legacy v11 behavior.

## Failure Policy

Fail early on missing files, ambiguous page ranges, unknown modes, missing call spec paths, invalid provider/model references, forced tool-choice with thinking enabled, or incomplete OCR directories. Do not add fallback rules just to keep the run moving. If a fallback or postprocess rule seems necessary, explain its purpose and blast radius before implementing it.
