# v11 prompts/zh_CN

本目录保存 Step2-Step5 与大模型交互相关的 prompt、输入 payload 结构和返回格式契约。

当前状态：

- 这些文件是从现有运行时代码整理出的外置规范副本。
- 现有 Python 代码尚未改为从本目录加载 prompt。
- 后续重构 `exam_import_v11.prompts.zh_CN` 时，必须以本目录为 prompt source of truth，再把旧代码中的内嵌 prompt 删除或改为读取文件。

文件：

- `step2_layout.md`：Step2 顶层题号范围检测。
- `step2_qa_alignment_contract.md`：Step2 `qa_alignment.json` 四模式主表契约，覆盖纯题面、混排、题面加答案、后导入答案。
- `step2_crop_algorithm.md`：Step2 范围检测后的裁剪图生成算法，包含同页跨栏 crop island 拆分规则。
- `step3_question_json.md`：Step3 单题题库 JSON 标准化，含 OCR 模式和 image-only 模式。
- `step35_latex_audit.md`：Step3.5 LaTeX 审计与格式修复。
- `step4_assets.md`：Step4 视觉资产归属与答案表格抽取。
- `step4_asset_placeholder_algorithm.md`：Step4 视觉资产占位对账算法设计，包含全量遍历、范围外追加和单图唯一归属规则。
- `step5_render.md`：Step5 MathJax 渲染输入输出格式；Step5 不调用大模型。
- `step_pipeline_processing_flow.md`：Step2-Step5 原先处理方式、上下游流转、失败边界和重构后的字段方向。

通用约束：

- 面向中文数学试题的 prompt 必须全部使用中文。
- prompt 不得中英混杂，除字段名、schema key、工具名、模型 API 关键字外。
- tool calling 模式必须明确要求模型调用工具，不得在普通正文输出 JSON、Markdown、代码块、解释或分析过程。
- 如果模型未返回强制 tool call，流程必须失败暴露。
- 这些文件只记录 prompt 和模型 I/O 契约，不保存任何一次运行的真实 OCR、图片 data URI、模型原始返回或临时产物。
