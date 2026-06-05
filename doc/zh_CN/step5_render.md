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
<block-root>/<run_id>_raw_units/question_packets/page_*.json
<block-root>/<run_id>_raw_units/answer_packets/page_*.json
<source-runs>/<run_id>/source_item.json
<source-runs>/<run_id>/{paper,answer,mixed}/ocr_blocks.json
<source-runs>/<run_id>/{paper,answer,mixed}/pages/page_*.png
<source-runs>/<run_id>/{paper,answer,mixed}/mineru_extract/images/*
```

Step5 读取题库字段：

```json
{
  "question_no": 1,
  "stem_latex": [],
  "options_latex": [],
  "answer_latex": [],
  "analysis_latex": [],
  "issues": []
}
```

## 输出

```text
<output-root>/<run_id>/index.html
<output-root>/index.html
<output-root>/<run_id>/assets/...
<output-root>/<run_id>/step2_crops/...
<output-root>/<run_id>/assets_manifest.json
```

命令行 stdout 输出：

```json
{
  "index": "<output-root>/index.html",
  "runs": ["<output-root>/<run_id>/index.html"],
  "assets_manifest": "<output-root>/<run_id>/assets_manifest.json",
  "asset_export": {
    "asset_reference_count": 0,
    "exported_asset_count": 0,
    "missing_asset_count": 0
  }
}
```

## 渲染规则

- MathJax 负责渲染 `$...$` 和 `$$...$$`。
- Step5 不修复 LaTeX；裸 `\frac`、`\sqrt` 等未包数学环境的问题应由 Step3 或 Step3.5 处理。
- `stem_latex`、`options_latex`、`answer_latex`、`analysis_latex` 中的 `<img src="...">`、`<table src="...">`、`<chart src="...">` 占位必须解析为对应资产，而不是作为普通文本显示。
- `<blank>`、`<choice_blank>` 和 `<options no="...">` 必须通过受控 adapter 转成渲染节点，不得原样泄露到最终页面。
- Step5 不会在题干末尾自动补渲染 `options_latex`；只有 stem_latex 中显式出现 `<options no="...">` 时，对应选项组才会显示。
- HTML 表格 `<table>...</table>` 保留并渲染。
- Step5 只导出题库中实际出现的资产标签，不预渲染未被引用的 source asset。
- Step5 必须先基于 Step2 packets 还原 `label -> source block` 映射，再从 source run 导出真实资产到 `assets/`。
- Step5 审阅页必须显示 `Step2 裁剪` 栏目；栏目图片从 `<step2_run_dir>/crops_manifest.json` 读取，并复制到 `<output-root>/<run_id>/step2_crops/` 后再渲染。
- 表格导出优先级固定为：`table_body -> <label>.html`；若 source run 同时存在表格截图，则一并保留为 `<label>.<suffix>`。
- 图片和图表只复制 source run 中已有的源图，不重裁剪，不重新推断归属；解析顺序优先使用原始 `IMG-SRC` 线索，其次才回退到 source block 的 `path` / `mineru_item.img_path`。
- 若某个标签无法映射到 source block，或 source block 没有可导出的资产文件，则保留 `Missing asset: <label>` 审阅块，并在 `assets_manifest.json` 记录缺失状态。

## 非职责

- 不调用 LLM。
- 不修改 question bank JSON。
- 不执行 LaTeX 审计或修复。
- 不重新分配资产归属。

