# V12 Runtime Notes

Use this reference before running commands against the bundled v12 runtime.

## Bundled Runtime

The skill carries its v12 runtime under:

```text
assets/v12_runtime/
```

The Python package is:

```text
assets/v12_runtime/exam_import/
```

Do not call v11 scripts unless the user explicitly asks for legacy v11 behavior.

## Core Commands

Validate a spec:

```powershell
python.exe <skill>/assets/v12_runtime/exam_import/cli/validate_spec.py --spec <spec.json>
```

Check prompt/schema/provider/call-spec contracts after any config or prompt change:

```powershell
python.exe <skill>/assets/v12_runtime/exam_import/cli/check_contracts.py
```

Prepare source runs:

```powershell
python.exe <skill>/assets/v12_runtime/exam_import/cli/source_import.py --spec <spec.json>
```

Run Step2-Step5:

```powershell
python.exe <skill>/assets/v12_runtime/exam_import/cli/run_pipeline.py --spec <spec.json>
```

Patch answers for an existing paper-only run:

```powershell
python.exe <skill>/assets/v12_runtime/exam_import/cli/source_import.py --spec <spec.json>
python.exe <skill>/assets/v12_runtime/exam_import/cli/answer_patch.py --spec <spec.json>
```

## Output Layout

Given `runs_root` and `run_id`, v12 writes:

```text
runs_root/
  source_runs/<run_id>/
  step2_exam_blocks/<run_id>_raw_units/
  question_bank/<run_id>/
  reviews_step3_5_latex_audit/<run_id>/
  rendered_question_bank_mathjax/<run_id>/
  reports/
```

Main evidence files:

- `reports/<run_id>_source_import_summary.json`
- `reports/<run_id>_v12_pipeline_evidence.json`
- `step2_exam_blocks/<run_id>_raw_units/qa_alignment.json`
- `step2_exam_blocks/<run_id>_raw_units/pipeline_summary.json`
- `question_bank/<run_id>/question_bank.json`
- `question_bank/<run_id>/question_bank.jsonl`
- `question_bank/<run_id>/summary.json`
- `reviews_step3_5_latex_audit/<run_id>/summary.json`
- `rendered_question_bank_mathjax/<run_id>/index.html`

## Runtime Rules

- `source_import.py` creates or reuses source parts according to `cache_policy`.
- `run_pipeline.py` is artifact-driven and fails on missing upstream artifacts instead of silently falling back.
- `question_bank.json` and `question_bank.jsonl` are written together by the centralized question-bank API.
- For Bailian/DashScope named tool calling, `enable_thinking` must stay false.
- Do not hand-edit final question-bank JSON to mask prompt, schema, model, or ordering problems.
