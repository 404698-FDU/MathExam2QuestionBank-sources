# v12 Runtime 实现细节说明

本文档只记录本次实现时加入的工程细节选择，不替代 prompt 契约，也不替代 `V12_REFACTOR_ARCHITECTURE.zh-CN.md`。

关于 prompt 文档、tool schema、运行时 schema 的分层放置规则，单独见 `V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`。本文件只记录当前实现为何这样落地，不重复承担结构规范文档职责。

## 1. 代码范围

本次已落地的是 v12 的独立基础层：

- `exam_import/core/`
- `exam_import/prompts/`
- `exam_import/schemas/`
- `exam_import/llm/`
- `exam_import/sources/`
- `tool_schemas/`
- `provider_config/`
- `call_specs/`
- `exam_import/cli/validate_spec.py`
- `exam_import/cli/check_contracts.py`
- `exam_import/cli/source_import.py`
- `exam_import/cli/run_pipeline.py`
- `exam_import/cli/answer_patch.py`
- `exam_import/steps/step2_layout.py`
- `exam_import/steps/step2_crop.py`
- `exam_import/steps/step2_runtime.py`
- `exam_import/steps/step3_question_json.py`
- `exam_import/steps/step35_normalize.py`
- `exam_import/steps/step4_assets.py`
- `exam_import/steps/step4_runtime.py`
- `exam_import/steps/step45_sync.py`
- `exam_import/steps/step5_render.py`
- `exam_import/render/`
- `tool_schemas/`
- `provider_config/`

当前包路径已统一为 `exam_import`。旧包名不再作为 v12 runtime 的导入路径使用。

## 2. 独立性约束

当前实现不 import `v11_runtime` 下任何 Python 模块，也不复用 v11 的 helper。

设计选择：

- 优先使用 Python 标准库；PDF 渲染/切页使用 `PyMuPDF`，MinerU HTTP 调用使用 `requests`。
- schema 校验使用本地 dataclass + 显式 validator。
- 不绑定 v11 现有 SDK、helper 或 CLI。

## 3. Prompt 读取规则

`PromptLoader` 当前支持三类 prompt 引用：

- `step3_question_json`
- `step3_question_json.prompt.md`
- `prompts/zh_CN/step3_question_json.prompt.md#system`

section 解析规则：

- 运行时 prompt 文件统一为 `*.prompt.md`。
- 只解析固定 section：`meta`、`system`、`user`。
- 不再按多级 heading path 提取子节。
- `#section` 只作为固定 section 的轻量别名，例如 `#system`、`#user`。

变量替换规则：

- 使用简单的 `{name}` 字面替换。
- 不使用整段 `str.format()`，避免误处理 prompt 中 JSON 示例的大括号。

## 4. Provider 与模型注册

当前 provider 配置已经外置到：

- `provider_config/providers/*.json`

当前内置 provider：

- `openai`
- `siliconflow`
- `dashscope`
- `bailian`

当前 model 配置已经外置到：

- `provider_config/models/*.json`

当前内置 model 只是最小配置集，用于把 v12 的配置层跑起来，不代表已经完成全量模型验证。

当前内置 model：

- `qwen3.7-plus`
- `qwen3.6-plus`
- `Qwen/Qwen3.6-27B`
- `qwen3.5-flash`
- `qwen-vl-max`
- `gpt-4.1-mini`

后续如果接入更多模型，应优先扩 `provider_config/models/*.json`，不要在业务步骤里写条件分支。

## 5. Token 估算

当前 `token_budget.py` 只是最小实现：

- 文本 token 估算使用 `len(text) / 4` 向上取整。
- 图片 token 估算使用固定 `1024 / image`。

这只是统一预算接口的占位实现，不应被当作精确计费器。后续如果 provider 需要更真实的视觉 token 估算，应单独升级这一层。

## 6. CLI 策略

`validate_spec.py` 已实现，可直接校验并打印规范化后的 v12 import spec。

`check_contracts.py` 已实现，可直接校验：

- `prompt_ref -> prompt 文件`
- 运行时 prompt 的 `meta/system/user` section
- `tool_schema_ref -> *.schema.json`
- `provider_config/providers/*.json`
- `provider_config/models/*.json`
- 核心 tool schema 的顶层 required 字段
- `call_specs/*.json -> provider/model/prompt/tool` 静态可解析性

`ImportSpec.llm` 当前支持两种 call spec 取法：

- `call_specs.<step_name>`：按步骤显式指定。
- `call_spec_path`：单路径 fallback，主要用于单步骤 probe。

当前还额外提供了独立 `call_spec_v1` 示例文件，放在：

- `call_specs/`

这些示例不依赖运行时代码内嵌默认值，目的是把“每一步怎么调用哪个 provider/model/tool schema”显式落成 JSON 文件；provider 和 model 的静态能力则独立放在 `provider_config/`。

百炼 / DashScope 的 tool calling 约束：

- 官方文档确认 OpenAI 兼容 Chat 支持 `tool_choice={"type":"function","function":{"name":"..."}}` 强制指定工具名，也支持 `tool_choice="required"`。
- 官方文档同时明确：思考模式模型不支持“强制指定某个工具”。
- 因此运行时代码现在会在 provider 配置层记录这条约束，并在请求构造阶段前置校验；如果对百炼/DashScope 传入命名式 `tool_choice` 且 `enable_thinking=true`，会直接失败。

Step2 本地模块当前状态：

- `step2_layout.py` 已实现 `qa_alignment_v2` 构建器和 `pipeline_summary` 构建器。
- `step2_crop.py` 已实现 crop island 规划算法。
- `sources/ocr_blocks.py` 已实现从 `ocr_blocks.json` 构建 layout items、标注页和 page packets。
- `step2_runtime.py` 已实现 Step2 LLM 调用、`qa_alignment.json` / `pipeline_summary.json` 写盘，以及实际 Step3 crop 图片生成。

Step3-Step5 当前状态：

- `step3_question_json.py` 已实现 image-only message 构造、tool calling request 组装、响应解析和 question bank 写入。
- `step35_normalize.py` 已实现本地格式审计提示、full normalize message 构造、响应解析和 Step3.5 结果写回。
- `step4_assets.py` 已实现视觉资产审查调用入口、答案表抽取调用入口，以及 question bank 同步逻辑。
- `step4_runtime.py` 已实现从 `qa_alignment + question_bank + packets` 构建 Step4 per-page compact input，并聚合多页 Step4 结果。
- `step5_render.py` 已实现本地 HTML 渲染输出，读取 Markdown 字段、导出真实资产并生成 MathJax 审阅页。

CLI 当前状态：

- `source_import.py` 已支持两条正式入口：
  - 导入现成的 prepared source part 目录，即显式复制 `ocr_blocks.json + pages/` 到 v12 `source_runs/<run_id>/<part>/`
  - 直接从原始 PDF 抽取 source run，按 part 显式选择 `local_pymupdf` 或 `mineru_vlm`
- `run_pipeline.py` 已实现 v12 的 artifact-driven 编排入口，当前可串起 Step2-Step5。
- `answer_patch.py` 已实现 mode preflight，并复用 `run_pipeline.py`。

以下 CLI 目前故意显式失败：

- 无。

## 7. 本轮顺手修正的 prompt 问题

发现并修正了两处 v12 prompt 自身矛盾：

- `step2_crop_algorithm.md` 仍引用 `question_groups.json` 和旧 `answer_items` 扁平字段。
- `step5_render.md` 仍引用旧版 `question_type`、`stem_latex`、`options_latex`、`answer_latex`、`analysis_latex`、`rubric_latex`。

这两处已同步改为 v12 当前审定结构。

## 8. Tool schema 实现方式

当前 tool schema 没有从 prompt Markdown 自动抽取，而是以独立 JSON 文件落在：

- `tool_schemas/*.schema.json`

这样做的原因：

- 先让 Step2-Step4 的 tool calling schema 有独立稳定落点，避免继续把 schema 藏在 Python 大字典里。
- prompt 文档中的 JSON code block 结构并不统一，直接自动抽取会把这轮实现范围拉大。

当前 `exam_import/llm/tool_schemas.py` 只做 schema alias 和文件读取，不再保存 schema 主体。

这属于过渡实现，不是最终目标。后续如果继续推进，应把 tool schema 改成从 prompt 文档或 schema 目录统一生成，并增加 prompt/schema 一致性检查，避免双份维护。

相关结构收敛方向已单独整理到 `V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`。

## 9. Markdown 渲染实现方式

Step5 当前使用已安装的开源 `markdown` Python 库，而不是手写 Markdown parser。

占位标签处理方式：

- 先把 `<blank>`、`<choice_blank>`、`<img src="...">`、`<table src="...">`、`<chart src="...">`、`<options no="...">` 转成受控 HTML。
- 再交给 `markdown.markdown()` 渲染。

资产解析策略：

- Step5 先扫描 `question_bank.json` 里实际出现的 `<img src>`、`<table src>`、`<chart src>` 标签。
- 再用 Step2 `question_packets/`、`answer_packets/` 的标签映射回 source run 对应 block，并从 source run 导出真实资产到 `rendered_question_bank_mathjax/<run_id>/assets/`。
- 图片和图表只复制源图。
- 表格优先导出 `table_body -> <label>.html`，同时如果 source run 有表格截图，也一并复制为 `<label>.<suffix>`，供渲染时按标签类型选择。
- 找不到真实资产时，渲染为明确的 `Missing asset: <label>` 审阅块，不吞掉占位，也不把标签直接裸露成正文。

## 10. 本轮补的细节

- 新增 `schemas/question_ranges.py`，用于承接 Step2 顶层题号范围工具输出，避免 Step2 真正接入时再临时发明结构。
- `exam_import/core/question_bank.py` 提供 `write_question_bank()`，集中负责 `question_bank.json` 和 `question_bank.jsonl` 双写；Step3、Step3.5 写回和 Step4 同步后都通过该 API 写入，避免两个 question bank 索引不一致。
- `run_pipeline.py` 当前走 artifact-driven 路线：优先消费已存在的 `qa_alignment.json`、Step3 crops、Step4 compact inputs 和 `question_bank.json`，不在缺失上游产物时静默降级。
- `source_import.py` 现在支持显式 extractor 配置：
  - `sources.paper_extractor`
  - `sources.answer_extractor`
  - `sources.mixed_extractor`
  默认是 `mineru_vlm`；只有配置成 `local_pymupdf` 时才会跳过 MinerU，不做静默 fallback。
- `schemas/import_spec.py` 新增 `mineru` 配置段，承接 `token_file`、`timeout`、`poll_interval`、`dpi`、`extract_retries`、`extract_retry_sleep`、`language`、`enable_formula`、`enable_table`、`enable_ocr`。
- `sources/page_assets.py` 负责 PDF 页码范围切分和页面渲染，`sources/mineru_extract.py` 负责本地/ MinerU 抽取，`sources/source_run_writer.py` 负责 source part 级别的写盘与复用规则。
- `step45_sync.py` 现在把 Step4.5 question bank 同步职责单独落成文件，避免继续把 Step4 调用入口和同步写回完全揉在一个模块里。
- `check_contracts.py` 当前只做“结构引用存在且关键字段未漂移”的机器校验，不负责运行真实模型调用，也不覆盖 dataclass 与 JSON Schema 的完全双向等价性。
- `provider_config/models/*.json` 现在把模型和 provider 的关系做成“一个模型可声明多个 provider”，例如 `qwen-vl-max`、`qwen3.5-flash` 同时允许 `dashscope` 和 `bailian`；`call_spec_loader.py` 只校验 provider 是否在该模型允许列表中，不再强绑成单 provider。
- `qwen3.6-plus` 已按百炼当前模型矩阵加入 model 配置，视为支持视觉输入、Function Calling 和结构化输出。
- `qwen3.7-plus` 已加入 model 配置，并已通过 Step3.5 文本规范化 `tool_calling` 实测；当前正式默认只将其用于 Step3.5，不用于 Step2/Step3/Step4 的视觉或范围识别调用。
- `step2_runtime.py` 使用 prompt 文档中的 `system prompt` 与 `user instruction template`，本地只补 `mode_rule`、`output_rule`、`final_rule` 三段变量，不再内嵌整份 Step2 prompt。
- `step4_runtime.py` 当前按“逐页 prompt、聚合结果”的方式运行；输入 compact JSON 在本地以数组形式落盘，数组元素仍然保持 prompt 约定的单页结构。
- `step4_assets.py` 现在按 prompt 契约发送多模态消息：
  - user content 第一段是“第 N 页标注图：”
  - 第二段是 annotated page `image_url`
  - 第三段才是替换完 `{compact_json}`、`{output_rule}`、`{final_rule}` 的文本指令
- 当前运行时 prompt 已拆成独立 `*.prompt.md`：
  - `step2_layout.prompt.md`
  - `step3_question_json.prompt.md`
  - `step35_latex_audit.prompt.md`
  - `step4_visual_assets.prompt.md`
  - `step4_answer_tables.prompt.md`
  旧的 `step2_layout.md`、`step3_question_json.md`、`step35_latex_audit.md`、`step4_assets.md` 已迁移到 `doc/zh_CN/`，只作为历史设计证据，不再作为运行时按 heading path 读取的源文件。
- `step3_question_json.py` 现在按“题面裁剪图 -> 答案/解析裁剪图 -> 文本指令”的顺序发送多模态消息，并在每张图前补明确的中文分组标签，避免多张 crop 输入时只靠隐式顺序判断。
- `step35_normalize.py` 的本地审计提示目前除了 `html_entity_present`、`embedded_newline`，还会额外标记：
  - `math_condition_split_across_text_connector`
  - `options_group_reference_mismatch`
  这些提示仍然只是模型全量审计的辅助线索，不限制 Step3.5 的修复范围。
- Step3.5 的 question bank 备份文件名现在统一为 `question_bank.before_step3_5_latex_audit.json`，与 `reviews_step3_5_latex_audit/` 目录和 `step35_latex_audit` prompt/call spec 命名保持一致，不再残留 `full_latex_normalize` 旧名字。
- `schemas/visual_asset.py` 的 `risks` 已从旧的 `IssueRecord(type/message)` 改为和 Step4 prompt/schema 一致的 `label/severity/reason` 结构。
- `render/asset_export.py` 当前是 Step5 资产导出层：它不重算归属，不改 question bank，只负责把已在题库中出现的占位标签解析成真实输出资产，并写 `assets_manifest.json` 供审阅和 evidence 使用。
- 已做一条无外部凭据的主链路 smoke：`raw PDF -> source_import(local_pymupdf) -> Step2(fake tool call over real runtime) -> Step3/3.5/4(fake) -> Step5 render`，用于证明 v12 当前能从 raw PDF 起步生成 `qa_alignment.json`、`question_bank.json`、`index.html` 和 evidence report。

## 11. Spec 生成入口

- 新增 `skills/exam-import/assets/import_spec.standard.template.json` 作为 v12 标准 spec 样板，保留 source、MinerU、LLM、cache、steps 的完整字段。
- 新增 `exam_import/cli/generate_spec.py`，只从标准样板读取 JSON，并用命令行参数覆盖字段后写出 spec；不扫描 PDF、不推断题型、不调用模型。
- `--source-only` 会移除 `llm` 段，用于先快速生成可给 `source_import.py` 使用的 source-only spec。
- 生成后仍通过 `schemas/import_spec.py` 做结构校验，避免写出缺少 mode 必需 source 的 spec。
