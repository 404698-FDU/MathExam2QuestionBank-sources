# Import Spec

Use this reference when converting a user request into a v12 machine-checkable import spec.

## Generator

Prefer generating new v12 specs from the standard template instead of copying an old run spec by hand:

```powershell
python.exe exam_import/cli/generate_spec.py `
  --run-id shanghai_2026_spring_paper_answer `
  --input-mode paper_plus_answer `
  --paper-pdf D:/path/to/paper.pdf `
  --answer-pdf D:/path/to/answer.pdf
```

For fast source import only, add `--source-only`; the generated spec omits `llm` because `source_import.py` does not need model settings.

## Minimal Shape

```json
{
  "schema_version": "import_spec_v2",
  "run_id": "shanghai_2026_spring_paper_answer",
  "input_mode": "paper_plus_answer",
  "runtime_root": ".",
  "runs_root": "runs/v12",
  "sources": {
    "paper_pdf": "D:/path/to/paper.pdf",
    "paper_prepared_dir": "",
    "paper_page_range": "",
    "paper_extractor": "mineru_vlm",
    "answer_pdf": "D:/path/to/answer.pdf",
    "answer_prepared_dir": "",
    "answer_page_range": "",
    "answer_extractor": "mineru_vlm",
    "mixed_pdf": "",
    "mixed_prepared_dir": "",
    "mixed_extractor": "mineru_vlm"
  },
  "source_rules": {
    "paper": "question paper PDF",
    "answer": "official answer and analysis PDF"
  },
  "mineru": {
    "token_file": "",
    "timeout": 900,
    "poll_interval": 10,
    "dpi": 144,
    "extract_retries": 3,
    "extract_retry_sleep": 60,
    "language": "ch",
    "enable_formula": true,
    "enable_table": true,
    "enable_ocr": true
  },
  "llm": {
    "provider": "dashscope",
    "primary_model": "qwen3.5-flash",
    "timeout": 180,
    "max_workers": 8,
    "enable_thinking": false,
    "call_specs": {
      "step2_question_ranges": "call_specs/step2_question_ranges.dashscope.qwen3.5-flash.tool_calling.json",
      "step3_question_json": "call_specs/step3_question_json.dashscope.qwen3.5-flash.tool_calling.json",
      "step35_latex_audit": "call_specs/step35_latex_audit.dashscope.qwen3.5-flash.tool_calling.json",
      "step4_visual_assets": "call_specs/step4_visual_assets.dashscope.qwen3.5-flash.tool_calling.json",
      "step4_answer_tables": "call_specs/step4_answer_tables.dashscope.qwen3.5-flash.tool_calling.json"
    }
  },
  "cache_policy": {
    "reuse_existing_ocr": false,
    "force_source": false,
    "force_pipeline": true
  },
  "steps": {
    "skip_step2": false,
    "skip_step3": false,
    "step3_5": true,
    "skip_step4": false,
    "skip_render": false
  }
}
```

## Input Modes

`pure_paper`: only question paper is available. Require `sources.paper_pdf` or `sources.paper_prepared_dir`.

`paper_plus_answer`: question paper and answer/analysis are separate logical parts. Require paper input and answer input. They may point to the same physical PDF when page ranges split one file into two parts.

`mixed`: question and answer/analysis are interleaved in one logical document. Require `sources.mixed_pdf` or `sources.mixed_prepared_dir`.

`answer_patch`: add an answer/analysis source to an existing paper-only run. Require existing `runs_root/source_runs/<run_id>/paper/` and new answer input.

## Source Inputs

Use exactly one form per logical part:

- PDF extraction: set `<part>_pdf`, optional page range, and extractor.
- Prepared source part: set `<part>_prepared_dir` when OCR blocks and pages already exist.

Valid extractors are `mineru_vlm` and `local_pymupdf`. `mineru_vlm` is the default Step1 extractor; use `local_pymupdf` only when the user explicitly wants local PDF text/image extraction.

## LLM Config

The v12 runtime separates three layers:

- `provider_config/`: provider and model static capability.
- `call_specs/`: per-step model call config.
- `import_spec_v2.llm`: which call specs this run uses.

For current Bailian/DashScope tool-calling call specs, keep `enable_thinking=false`; the provider config enforces this for named `tool_choice`.

## Evidence Required After A Run

Report these paths and counts when available:

- `runs_root/reports/<run_id>_source_import_summary.json`
- `runs_root/step2_exam_blocks/<run_id>_raw_units/qa_alignment.json`
- `runs_root/step2_exam_blocks/<run_id>_raw_units/pipeline_summary.json`
- `runs_root/question_bank/<run_id>/summary.json`
- `runs_root/reviews_step3_5_latex_audit/<run_id>/summary.json`
- Step4 sync summary from `step4_question_bank_sync.json`
- render entry: `runs_root/rendered_question_bank_mathjax/<run_id>/index.html`
- failed question numbers and `errors.json` files
