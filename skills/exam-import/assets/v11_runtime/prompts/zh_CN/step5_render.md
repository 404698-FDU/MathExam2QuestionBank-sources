# Step5 Render Contract

来源代码：`step5_vlm_html_mathjax_render.py`

Step5 不调用大模型，没有 prompt。此文件保存 Step5 的输入和输出格式，方便完整保存 Step2-Step5 与模型相关或渲染相关的接口契约。

## 输入

CLI 参数：

```powershell
python.exe step5_vlm_html_mathjax_render.py `
  --run-id <run_id> `
  --qb-root <runs>/question_bank `
  --asset-root <runs>/rendered_question_bank_mathjax `
  --block-root <runs>/step2_exam_blocks `
  --source-runs <runs>/source_runs `
  --output-root <runs>/rendered_question_bank_mathjax
```

输入文件：

```text
<qb-root>/<run_id>/question_bank.json
<qb-root>/<run_id>/summary.json
<block-root>/<run_id>_raw_units/pipeline_summary.json
<block-root>/<run_id>_raw_units/qa_alignment.json
<source-runs>/<run_id>/source_item.json
<source-runs>/<run_id>/{paper,answer}/pages/page_*.png
```

Step5 读取题库字段：

```json
{
  "question_no": 1,
  "question_type": "fill_blank|single_choice|multiple_choice|solution|unknown",
  "stem_latex": [],
  "options_latex": {"A": [], "B": [], "C": [], "D": []},
  "answer_latex": [],
  "analysis_latex": [],
  "rubric_latex": [],
  "visual_assets": [],
  "option_asset_placeholders": {}
}
```

## 输出

```text
<output-root>/<run_id>/index.html
<output-root>/index.html
<output-root>/<run_id>/assets/...
```

命令行 stdout 输出：

```json
{
  "index": "<output-root>/index.html",
  "runs": [
    "<output-root>/<run_id>/index.html"
  ]
}
```

## 渲染规则

- MathJax 负责渲染 `$...$` 和 `$$...$$`。
- Step5 不修复 LaTeX；裸 `\frac`、`\sqrt` 等未包数学环境的问题应由 Step3 或 Step3.5 处理。
- 图片标号如 `Q-V02-P01`、`A-V03-P01`、`M-V01-P01` 应解析为对应资产图片。
- `visual_assets` 和 `option_asset_placeholders` 中有同名资产时，不应把图片标号作为普通文本显示。
- HTML 表格 `<table>...</table>` 保留并渲染。

## 非职责

- 不调用 LLM。
- 不修改 question bank JSON。
- 不执行 LaTeX 审计或修复。
- 不重新分配资产归属。
