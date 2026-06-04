# v12 prompts/zh_CN

本目录只保存 Step2-Step5 与大模型交互相关的运行时 prompt。

当前状态：

- `*.prompt.md` 是 v12 runtime 实际读取的轻量 prompt 文件，固定使用 `meta/system/user` 三段结构。
- 算法、流程、历史契约和人工审阅文档放在 `../../doc/zh_CN/`，不由 `PromptLoader` 读取。
- `check_contracts.py` 只检查 `*.prompt.md` 及其 `meta.source_doc` 是否存在，不把 `doc/zh_CN` 注册为 prompt。
- v12 运行代码必须从本目录加载 prompt，不得再内嵌大段 prompt。
- `exam_import.prompts` 必须以本目录为 prompt source of truth。
- prompt、tool schema、运行时 schema 的分层放置规则见上级文档 `V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`。

文件：

- `step2_layout.prompt.md`：Step2 顶层题号范围检测运行时 prompt。
- `step3_question_json.prompt.md`：Step3 image-only 单题结构化运行时 prompt。
- `step35_latex_audit.prompt.md`：Step3.5 全量格式规范化运行时 prompt。
- `step4_visual_assets.prompt.md`：Step4 视觉资产占位对账运行时 prompt。
- `step4_answer_tables.prompt.md`：Step4 答案页表格抽取运行时 prompt。

历史证据：

- `../../doc/zh_CN/step2_layout.md`：Step2 顶层题号范围检测旧长版说明、payload 和返回格式契约。
- `../../doc/zh_CN/step2_qa_alignment_contract.md`：Step2 `qa_alignment.json` 四模式主表契约，覆盖纯题面、混排、题面加答案、后导入答案。
- `../../doc/zh_CN/step2_crop_algorithm.md`：Step2 范围检测后的裁剪图生成算法，包含同页跨栏 crop island 拆分规则。
- `../../doc/zh_CN/step3_question_json.md`：Step3 单题题库 JSON 标准化旧长版说明和 schema 契约。
- `../../doc/zh_CN/step35_latex_audit.md`：Step3.5 LaTeX 审计与格式修复旧长版说明和 schema 契约。
- `../../doc/zh_CN/step4_assets.md`：Step4 视觉资产归属与答案表格抽取旧长版说明和 schema 契约。
- `../../doc/zh_CN/step4_asset_placeholder_algorithm.md`：Step4 视觉资产占位对账算法设计，包含全量遍历、范围外追加和单图唯一归属规则。
- `../../doc/zh_CN/step5_render.md`：Step5 MathJax 渲染输入输出格式；Step5 不调用大模型。
- `../../doc/zh_CN/step_pipeline_processing_flow.md`：Step2-Step5 原先处理方式、上下游流转、失败边界和重构后的字段方向。

通用约束：

- 面向中文数学试题的 prompt 必须全部使用中文。
- prompt 不得中英混杂，除字段名、schema key、工具名、模型 API 关键字外。
- tool calling 模式必须明确要求模型调用工具，不得在普通正文输出 JSON、Markdown、代码块、解释或分析过程。
- 如果模型未返回强制 tool call，流程必须失败暴露。
- 这些文件只记录 prompt 和模型 I/O 契约，不保存任何一次运行的真实 OCR、图片 data URI、模型原始返回或临时产物。
