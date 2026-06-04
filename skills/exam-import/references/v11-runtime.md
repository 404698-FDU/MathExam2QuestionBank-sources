# V11 Runtime Notes

Use this reference before running commands against the bundled v11 runtime or an explicitly selected external v11 worktree.

## Bundled Runtime

This Skill carries its own v11 runtime under:

```text
assets/v11_runtime/
```

`scripts/validate_import_spec.py` defaults to this bundled runtime when `v11_root` is omitted. Pass `--v11-root <path>` only when the user explicitly wants to run a different local v11 worktree.

## Expected Files

The v11 root should contain:

- `run_pipeline.py`
- `run_answer_patch.py`
- `step2_layout.py`
- `step3_question2json.py`
- `step3_5_question_json_audit_fix.py`
- `step4_asset_distribution.py`
- `step5_vlm_html_mathjax_render.py`
- `batch_import_2017_2026.py`
- `exam_agent_pipeline.py`
- `question_bank_app.py`
- `step0_wechat_import/`

The default run root is `<v11_root>/runs`.

## Source Run Layout

New imports should create:

```text
runs/source_runs/<run_id>/
  source_item.json
  paper/ocr_blocks.json
  answer/ocr_blocks.json   # only for paper_plus_answer_file
```

Mixed imports use `paper/ocr_blocks.json` only; Step2 is forced to `mixed`.

## Main Pipeline Command

The core command shape is:

```powershell
python.exe <v11_root>/run_pipeline.py `
  --source-runs-root <runs_root>/source_runs `
  --runs-root <runs_root>/step2_exam_blocks `
  --qb-root <runs_root>/question_bank `
  --render-root <runs_root>/rendered_question_bank_mathjax `
  --step3-5-root <runs_root>/reviews_step3_5_latex_audit `
  --run-id <run_id> `
  --mode <input_mode> `
  --model <primary_model> `
  --timeout <seconds> `
  --max-workers <n> `
  --force
```

Add repeated `--worker-model <model>` and repeated `--model-tpm-limit MODEL=LIMIT` for multi-model TPM control.

## Answer Patch Command

Use this only when the existing run was originally imported as `pure_paper`:

```powershell
python.exe <v11_root>/run_answer_patch.py `
  --run-id <run_id> `
  --answer-pdf <answer.pdf> `
  --answer-page-range <range> `
  --model <model> `
  --timeout 180 `
  --max-workers 8
```

`run_answer_patch.py` preserves existing pure-paper Step2 ranges, rebuilds answer ranges, and then calls `run_pipeline.py --skip-step2`.

## Strictness

Do not silently reuse OCR or Step outputs unless the spec allows it. If `cache_policy.reuse_existing_ocr` is false and a source part already has OCR, force source extraction or stop and ask.
