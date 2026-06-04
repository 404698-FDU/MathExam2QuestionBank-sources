# exam-import 重构记录

## 2026-06-05

### 禁止 Step3/Step4 自造资产标签

修改范围：

- 修改 `prompts/zh_CN/step3_question_json.prompt.md`
- 修改 `prompts/zh_CN/step4_visual_assets.prompt.md`
- 修改 `exam_import/steps/step3_question_json.py`
- 修改 `exam_import/cli/run_pipeline.py`
- 修改 `exam_import/steps/step4_runtime.py`

修改原因：

- 已完成的导入 smoke test 中，Step5 渲染发现 7 个资产标签缺失映射，原因是模型输出了 Step2 source packets 中不存在的标签，例如 `Q-V02-P01`、`M-V02-T01`。
- Step5 不应猜测标签；Step3/Step4 也不应发明资产标签。

影响：

- Step3 prompt 明确禁止自造 `<img/table/chart src>` 标签。
- Step3 结果写入前会基于 `qa_alignment` 的真实标签集合移除未知资产占位，并追加 `content_missing` warning issue。
- Step4 prompt 明确要求 `assets[].label` 和 placeholder `src` 只能来自 compact input 的 `assets[].label`。
- Step4 runtime 会把模型返回但 compact input 中不存在的资产标签，或 placeholder `src` 与资产标签不一致的结果转为 warning risk，不同步到 question bank。

验证：

- 已运行默认模板 spec 校验，结果通过。
- 已运行 contract check，结果通过。
- 已对 `exam_import/**/*.py` 执行源码级编译检查，结果通过：56 个 Python 文件。

### 收敛 PromptLoader 职责为只读取模型 prompt

修改范围：

- 修改 `exam_import/prompts/registry.py`
- 修改 `exam_import/cli/check_contracts.py`
- 修改 `prompts/zh_CN/README.md`

修改原因：

- `PromptLoader` 不应管理算法文档、流程文档或历史契约文档的存放。
- 运行时真正发送给模型的只有 `*.prompt.md`，其他文档只应作为 `meta.source_doc` 的人工证据和 contract check 对象。

影响：

- prompt registry 只保留 5 个模型 prompt。
- contract check 单独检查 prompt `meta.source_doc` 是否存在且仍位于 runtime 根目录内。
- `PromptLoader` 不再通过 `../../doc/zh_CN/...` 读取非 prompt 文档。

验证：

- 已运行默认模板 spec 校验，结果通过。
- 已运行本次导入 run spec 校验，结果通过。
- 已运行 contract check，结果通过：prompt_refs=20、prompt_source_docs=5、providers=3、models=6、tool_schema_aliases=10、call_specs=5。
- 已对 `exam_import/**/*.py` 执行源码级编译检查，结果通过：56 个 Python 文件。

### 修复 qwen3.5-flash 模型配置 JSON

修改范围：

- 修改 `provider_config/models/qwen3.5-flash.json`
- 修改 `provider_config/models/qwen-vl-max.json`
- 修改 `call_specs/*.json`
- 重命名默认 `call_specs/*.json` 为 DashScope / `qwen3.5-flash` 文件名
- 修改 `exam_import/prompts/registry.py`
- 修改 `skills/exam-import/assets/import_spec.template.json`
- 修改 `skills/exam-import/references/import-spec.md`

修改原因：

- 用户要求本次导入每一步都调用 DashScope 的 `qwen3.5-flash`。
- 本地读取配置时发现 `providers` 数组中存在 `"dashscope"x` 语法错误，会阻断模型配置解析。

影响：

- `qwen3.5-flash` 可被 `dashscope` provider 正常解析。
- 根目录默认 call specs 和 skill 模板统一为 DashScope / `qwen3.5-flash`。
- 算法/流程类 prompt 引用改为读取 `doc/zh_CN/`，避免与运行时 `*.prompt.md` 混放。
- Step2 范围构建器在模型误把页图/视觉标签填入反向 `end_label` 时，不再把该标签作为文本边界，而是保留为视觉标签；合并标签时核心范围保持在前，范围外视觉标签追加到后面。
- 修复 Step4 runtime 汇总时把 `Step4SyncSummary` dataclass 当作 mapping 展开的错误，改为显式 `to_dict()`。
- Step4 答案表 schema 对由 `role` 唯一决定的 `target_field` 做确定性归一化，避免模型把 `analysis_table` 的目标字段误填为答案字段时中断；非答案表若仍携带 entries 继续失败。

验证：

- 待随本次导入前置执行 contract check。

## 2026-06-04

### 将 V12 runtime 固化为 sources 仓库并默认 Step1 调用 MinerU

修改范围：

- 新增仓库根 `.gitignore`
- 复制 `skills/exam-import/` 到仓库内
- 新增 `skills/exam-import/assets/import_spec.template.json`
- 修改 `exam_import/schemas/import_spec.py`
- 修改 `skills/exam-import/SKILL.md`
- 修改 `skills/exam-import/references/import-spec.md`
- 修改 `V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- 用户已将 V12 runtime 迁移到 `code/sources`，要求把该目录上传为仓库，并在仓库内保留原 PDF 导入 skill。
- 用户要求默认配置中 Step1 调用 MinerU，因此 `paper_extractor`、`answer_extractor`、`mixed_extractor` 的默认值和模板都改为 `mineru_vlm`。

影响：

- 仓库根目录是 V12 runtime，skill 文档不再指向 `assets/v12_runtime`，而是指向仓库根 runtime。
- 复制后的 skill 排除旧 `tmp`、`__pycache__`、旧 zip、知识点分类导出和微信样例资产，避免把历史运行产物带入新仓库。
- 本地 PyMuPDF 抽取仍保留为显式配置项 `local_pymupdf`，但不再是默认 Step1。

验证：

- 已运行 `python.exe exam_import/cli/validate_spec.py --spec skills/exam-import/assets/import_spec.template.json`，模板可解析为 `import_spec_v2`，默认 Step1 extractor 为 `mineru_vlm`。
- 已运行 `python.exe exam_import/cli/check_contracts.py`，结果通过：prompts=25、providers=4、models=6、tool_schemas=10、call_specs=5。
- 已对 `exam_import/**/*.py` 执行源码级编译检查，结果通过：56 个 Python 文件。

### 将 exam-import skill 默认调用固化为 v12 解析流程

修改范围：

- 修改 `SKILL.md`
- 修改 `agents/openai.yaml`
- 修改 `assets/import_spec.template.json`
- 修改 `references/import-spec.md`
- 新增 `references/v12-runtime.md`
- 修改 `assets/v12_runtime/call_specs/README.zh-CN.md`

修改原因：

- 现有 skill 仍以 v11 为默认运行入口，用户要求把“调用 v12 解析”固化为 skill。
- v12 已有稳定入口：`validate_spec.py`、`source_import.py`、`run_pipeline.py`、`answer_patch.py`，skill 应直接围绕这些入口组织，而不是继续生成 v11 命令。

影响：

- skill 触发描述现在明确指向 v12 数学试卷导入解析。
- 默认 spec 模板改为 `import_spec_v2`，输入模式改为 `pure_paper`、`mixed`、`paper_plus_answer`、`answer_patch`。
- skill 工作流固定为：生成 spec、校验 spec、必要时检查 v12 contracts、导入 source run、运行 Step2-Step5、报告 evidence。
- v11 说明保留为 legacy reference，只有用户明确要求 v11 时才读取。

验证：

- 已运行 v12 spec 模板校验，`assets/import_spec.template.json` 可被 `validate_spec.py` 解析为 `import_spec_v2`。
- 已确认模板中的 5 个 call spec 路径均存在。
- 已运行 v12 contract check，结果通过：prompts=25、providers=4、models=6、tool_schemas=10、call_specs=5。
- 已扫描 skill 主文档、UI 元数据、v12 reference 和 spec 模板，默认入口不再残留 v11；仅保留“用户明确要求 legacy v11 时才读取”的说明。

### 同步百炼 / DashScope 工具调用约束并补充 qwen3.6-plus、qwen3.7-plus

修改范围：

- 修改 `assets/v12_runtime/exam_import/llm/providers.py`
- 修改 `assets/v12_runtime/exam_import/llm/request_builder.py`
- 修改 `assets/v12_runtime/provider_config/providers/dashscope.json`
- 修改 `assets/v12_runtime/provider_config/providers/bailian.json`
- 新增 `assets/v12_runtime/provider_config/models/qwen3.6-plus.json`
- 新增 `assets/v12_runtime/provider_config/models/qwen3.7-plus.json`
- 修改 `assets/v12_runtime/provider_config/README.zh-CN.md`
- 修改 `assets/v12_runtime/call_specs/README.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`

修改原因：

- 根据阿里云百炼官方文档，OpenAI 兼容 Chat 支持命名式 `tool_choice` 和 `tool_choice=\"required\"`，但思考模式模型不支持“强制指定某个工具”。
- 当前 Step2-Step4 都使用命名式 `tool_choice`，需要把这条官方约束固化到 provider 配置和请求构造阶段，而不是只写在 prompt 里。
- 需要把百炼当前常用模型 `qwen3.6-plus`、`qwen3.7-plus` 补进 v12 的 model 配置层。

影响：

- `dashscope` / `bailian` provider config 现在显式记录：
  - 支持命名式 `tool_choice`
  - 支持 `tool_choice=\"required\"`
  - 命名式 `tool_choice` 需要 `enable_thinking=false`
- `request_builder.py` 现在会在本地前置校验这条约束，不等接口报错。
- `qwen3.6-plus` 已加入 model 配置，并按官方能力矩阵开放视觉、Function Calling 与结构化输出。
- `qwen3.7-plus` 已加入 model 配置；由于当前百炼公开总能力矩阵未明确给出其 Function Calling/结构化输出支持状态，暂按保守配置处理，只作为可识别模型 ID 暴露。

验证：

- 本轮将重新执行 `compile()` 与 `check_contracts.py`。

### 外置 provider/model 静态配置到 `provider_config/`

修改范围：

- 新增 `assets/v12_runtime/provider_config/README.zh-CN.md`
- 新增 `assets/v12_runtime/provider_config/providers/*.json`
- 新增 `assets/v12_runtime/provider_config/models/*.json`
- 修改 `assets/v12_runtime/exam_import/core/paths.py`
- 修改 `assets/v12_runtime/exam_import/llm/providers.py`
- 修改 `assets/v12_runtime/exam_import/llm/model_configs.py`
- 修改 `assets/v12_runtime/exam_import/cli/check_contracts.py`
- 修改 `assets/v12_runtime/call_specs/README.zh-CN.md`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- 之前 provider 和 model 的静态配置直接硬编码在 `providers.py` 和 `model_configs.py` 中，结构上仍然偏“代码即配置”，和已经外置的 `call_specs/`、`tool_schemas/` 不一致。
- 用户要求把 model 和 provider 的具体配置拆到独立目录，这样后续新增模型或切 provider 时，不需要再改 Python 字面量。

影响：

- v12 现在新增 `assets/v12_runtime/provider_config/`：
  - `providers/*.json` 保存厂商级接入规则。
  - `models/*.json` 保存模型级静态能力。
- `exam_import/llm/providers.py` 和 `model_configs.py` 现在只保留 dataclass、JSON 读取和运行时解析逻辑。
- `check_contracts.py` 现在会校验：
  - `provider_config/providers/*.json`
  - `provider_config/models/*.json`
  - model 声明的 provider 是否真实存在
- `call_spec.json` 继续只描述单次调用，不再承担 provider/model 静态能力定义。

验证：

- 已对 `assets/v12_runtime/exam_import/**/*.py` 执行源码级 compile 检查。
- 已运行 v12 契约检查，确认 provider/model 配置、tool schema、call spec 可以同时解析。

### 统一 v12 包路径、旧契约文档归档和 question bank 写入 API

修改范围：

- 将 `assets/v12_runtime/scripts/` 迁移为 `assets/v12_runtime/exam_import/`。
- 批量同步 v12 运行代码、call spec、prompt 和架构文档中的包路径引用为 `exam_import`。
- 将旧长版 prompt 契约文档迁移到 `assets/v12_runtime/doc/zh_CN/`：
  - `step2_layout.md`
  - `step3_question_json.md`
  - `step35_latex_audit.md`
  - `step4_assets.md`
- 修改 `assets/v12_runtime/exam_import/cli/check_contracts.py`，只校验新的运行时 prompt `meta/system/user` 三段、tool schema 和 call spec，不再把旧长文档作为机器约束源。
- 新增 `assets/v12_runtime/exam_import/core/question_bank.py`。
- 修改 `assets/v12_runtime/exam_import/steps/step3_question_json.py`、`step35_normalize.py`、`step4_assets.py`，统一通过 `write_question_bank()` 写入主索引。
- 修改 `assets/v12_runtime/prompts/zh_CN/*.prompt.md` 的 `source_doc`，指向 `../../doc/zh_CN/` 下的历史证据文档。
- 修改 `assets/v12_runtime/prompts/zh_CN/README.md`、`V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`、`V12_REFACTOR_ARCHITECTURE.zh-CN.md`、`V12_IMPLEMENTATION_NOTES.zh-CN.md`，同步新的目录职责。

修改原因：

- v12 runtime 的导入路径应收敛为 `exam_import`，避免 `scripts` 目录名和旧包名继续参与运行时结构。
- 旧长版 prompt 契约文档只作为审计证据保留，不应继续作为 `check_contracts.py` 的硬约束来源。
- `question_bank.json` 和 `question_bank.jsonl` 双写可以保留，但必须集中到一个 API，避免 Step3、Step3.5、Step4 各自实现时产生索引漂移。

影响：

- v12 代码导入统一使用 `exam_import.*`。
- `prompts/zh_CN/` 只保留运行时 prompt 和当前算法/流转说明；历史契约文档进入 `doc/zh_CN/`。
- 主 question bank 索引只由 `exam_import.core.question_bank.write_question_bank()` 写入。
- Step3.5 的 `normalized_question_bank.json`、`merged_question_bank.json/jsonl` 仍作为 review 产物独立写入，不属于主索引双写 API 的职责。

验证：

- 已对 `assets/v12_runtime/exam_import/**/*.py` 执行源码级 compile 检查，结果通过：56 个 Python 文件。
- 已运行 v12 契约检查，结果通过：prompts=25、tool_schemas=10、call_specs=5。
- 已做 PromptLoader 烟测，能读取 `step3_question_json.prompt.md`，并能解析指向 `../../doc/zh_CN/step3_question_json.md` 的 `source_doc`。
- 已确认 `assets/v12_runtime/scripts/` 不存在，`assets/v12_runtime/exam_import/` 存在。
- 已确认主索引 `question_bank.json/jsonl` 的写入集中在 `exam_import/core/question_bank.py`。

### 统一 v12 运行时 prompt 形态为独立 `*.prompt.md`

修改范围：

- 新增 `assets/v12_runtime/prompts/zh_CN/step2_layout.prompt.md`
- 新增 `assets/v12_runtime/prompts/zh_CN/step3_question_json.prompt.md`
- 新增 `assets/v12_runtime/prompts/zh_CN/step35_latex_audit.prompt.md`
- 新增 `assets/v12_runtime/prompts/zh_CN/step4_visual_assets.prompt.md`
- 新增 `assets/v12_runtime/prompts/zh_CN/step4_answer_tables.prompt.md`
- 修改 `assets/v12_runtime/exam_import_v12/prompts/loader.py`
- 修改 `assets/v12_runtime/exam_import_v12/prompts/registry.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/step2_runtime.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/step3_question_json.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/step35_normalize.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/step4_assets.py`
- 修改 `assets/v12_runtime/exam_import_v12/cli/check_contracts.py`
- 修改 `assets/v12_runtime/call_specs/step4_visual_assets.bailian.qwen-vl-max.tool_calling.json`
- 修改 `assets/v12_runtime/call_specs/step4_answer_tables.bailian.qwen-vl-max.tool_calling.json`
- 修改 `assets/v12_runtime/prompts/zh_CN/README.md`
- 修改 `assets/v12_runtime/V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- 之前运行时 prompt 和说明/契约文档混在同一批 `*.md` 文件里，`PromptLoader` 需要支持多级 heading path 才能抽出实际 system/user 提示，结构偏重。
- Step4 还要在一个文件里区分两个模型任务，导致 loader 和合同检查都被迫理解文档组织细节，而不是只理解“一个调用单元对应一个 prompt 文件”。
- 这次目标是只改形式，不改 prompt 实际内容：把运行时真正消费的 system/user 文本拆到独立 `*.prompt.md`，旧长文档保留为说明和 schema 契约。

影响：

- 运行时 prompt 现在统一采用固定结构：
  - `## meta`
  - `## system`
  - `## user`
- `PromptLoader` 不再实现按多级 heading path 取 prompt 的逻辑，只保留：
  - 整文件读取
  - 固定 section 读取
  - 简单变量替换
  - fenced block 提取
- Step4 prompt 拆成两个独立文件：
  - `step4_visual_assets.prompt.md`
  - `step4_answer_tables.prompt.md`
- Step4 两个 call spec 现在直接引用对应 prompt_ref，不再共用 `step4_assets` 后再按 heading path 分流。
- 旧的 `step2_layout.md`、`step3_question_json.md`、`step35_latex_audit.md`、`step4_assets.md` 继续保留为人工审阅文档，不再作为运行时 prompt 源。

验证：

- 本轮将额外比对新旧 prompt 的 system/user 文本是否一致。
- `check_contracts.py` 将改为检查新 prompt 的 `meta/system/user` 三段，而不是旧 heading path。

### 收紧 Step3 图片消息分组并增强 Step3.5 本地审计提示

修改范围：

- 修改 `assets/v12_runtime/exam_import_v12/steps/step3_question_json.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/step35_normalize.py`
- 修改 `assets/v12_runtime/prompts/zh_CN/step35_latex_audit.md`
- 修改 `assets/v12_runtime/prompts/zh_CN/step_pipeline_processing_flow.md`
- 修改 `assets/v12_runtime/prompts/zh_CN/step4_assets.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- Step3 之前虽然已经发送了题面图和答案图，但多张 crop 只靠隐式顺序，没有显式“题面裁剪图 / 答案解析裁剪图”分组标签，和前面 Step2、Step4 的消息组织方式不够一致。
- Step3.5 prompt 已经把“数学条件被中文连接词切断”“选项组引用一致性”写进了全量规范化规则，但本地 `audit_record()` 之前只会提示 HTML 实体和换行，辅助信号偏弱。
- `Step3.5` 还有几处残留 `full_latex_normalize` 和旧脚本名，和当前 `step35_latex_audit` / `reviews_step3_5_latex_audit` 命名不一致。

影响：

- `step3_question_json.py` 现在会按顺序发送：
  - `第 N 题题面裁剪图 i：`
  - 对应 `image_url`
  - `第 N 题答案/解析裁剪图 j：`
  - 对应 `image_url`
  - 最终文本指令
- `step35_normalize.py` 现在新增两类本地审计提示：
  - `math_condition_split_across_text_connector`
  - `options_group_reference_mismatch`
- Step3.5 相关代码和文档里的命名现在统一为：
  - 源码：`step35_normalize.py`
  - prompt：`step35_latex_audit`
  - review 目录：`reviews_step3_5_latex_audit/`
  - 备份文件：`question_bank.before_step3_5_latex_audit.json`
- 这两类提示只作为 Step3.5 full 审计的辅助输入，不改变“模型必须全量检查整条记录”的边界。

验证：

- 已做 `step3_message_smoke`：
  - 确认 Step3 user content 中题面图、答案图前都有显式中文标签
  - 确认最终文本指令仍保留在最后一段
- 已做 `step35_audit_smoke`：
  - 构造 `$...$ 且 $...$` 文本后，成功命中 `math_condition_split_across_text_connector`
  - 构造 `<options no=\"2\">` 与 `options_markdown.no=\"1\"` 不一致记录后，成功命中 `options_group_reference_mismatch`
- 已重新执行 `compile()` 源码检查，结果通过。

### 修正 Step4 实际消息形态与 prompt 契约不一致的问题

修改范围：

- 修改 `assets/v12_runtime/exam_import_v12/steps/step4_assets.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/step4_runtime.py`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- `step4_assets.md` 明确规定 Step4 两个模型调用都必须发送：
  - “第 N 页标注图：”文本
  - annotated page `image_url`
  - 替换完变量的文本指令
- 但此前运行时代码实际上只发送了纯文本 `compact_json`，没有发 annotated image，也没有把 `{output_rule}`、`{final_rule}` 真正替换掉。

影响：

- `call_visual_asset_review()` 和 `call_answer_table_review()` 现在都要求传入 `annotated_page_image`。
- 实际发送给模型的 user content 改成多段数组，和 prompt 文档一致。
- Step4 运行时内部新增 page job 封装，保留：
  - 持久化的 compact JSON 产物
  - 运行时单页 annotated image 路径
- 如果 annotated page 图片缺失，现在会直接失败暴露。

验证：

- 已做 `step4_message_contract_smoke`：
  - `call_visual_asset_review()` 的消息中确认存在 `image_url`
  - `call_answer_table_review()` 的消息中确认存在 `image_url`
  - 两者的文本指令中都已不再残留 `{output_rule}`、`{final_rule}`
  - 两者都能完成本地 fake tool-call 解析

### 增加 raw PDF 起步的 v12 主链路 smoke

修改范围：

- 无新增源码文件；使用现有 `source_import.py`、`step2_runtime.py`、`run_pipeline.py`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- 之前虽然分别验证过 raw PDF source import、本地 Step2、render，但还缺少一条“从 raw PDF 起步，一路到 render”的串联证据。

影响：

- 补充了一条无外部凭据 smoke：
  - raw PDF 用 `local_pymupdf` 抽取 source run
  - Step2 走真实 runtime + fake tool call
  - Step3 / Step3.5 / Step4 用本地 fake 调用
  - Step5 真实渲染

验证：

- smoke 输出目录：`tmp/v12_rawpdf_pipeline_smoke`
- 成功生成：
  - `source_runs/rawpdf_pipeline_smoke/paper/ocr_blocks.json`
  - `step2_exam_blocks/rawpdf_pipeline_smoke_raw_units/qa_alignment.json`
  - `question_bank/rawpdf_pipeline_smoke/question_bank.json`
  - `rendered_question_bank_mathjax/rawpdf_pipeline_smoke/index.html`
  - `reports/rawpdf_pipeline_smoke_v12_pipeline_evidence.json`

### 补齐 v12 的 call_spec 示例文件并放开百炼 provider 绑定

修改范围：

- 新增 `assets/v12_runtime/call_specs/README.zh-CN.md`
- 新增 `assets/v12_runtime/call_specs/step2_question_ranges.bailian.qwen-vl-max.tool_calling.json`
- 新增 `assets/v12_runtime/call_specs/step3_question_json.bailian.qwen-vl-max.tool_calling.json`
- 新增 `assets/v12_runtime/call_specs/step35_latex_audit.bailian.qwen3.5-flash.tool_calling.json`
- 新增 `assets/v12_runtime/call_specs/step4_visual_assets.bailian.qwen-vl-max.tool_calling.json`
- 新增 `assets/v12_runtime/call_specs/step4_answer_tables.bailian.qwen-vl-max.tool_calling.json`
- 修改 `assets/v12_runtime/exam_import_v12/llm/model_configs.py`
- 修改 `assets/v12_runtime/exam_import_v12/llm/call_spec_loader.py`
- 修改 `assets/v12_runtime/V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- 用户之前已经明确要求“每次调用配置应当是调用时输入一个 json 去阅读”，但 v12 之前只有 `call_spec.py` 结构和 `import_spec.llm.call_specs` 路径映射，没有正式示例文件。
- 同时 `providers.py` 虽然已经注册了 `bailian`，但 `model_configs.py` 里 `qwen-vl-max` 和 `qwen3.5-flash` 只允许 `dashscope`，导致百炼 provider 在配置层实际上不可用。

影响：

- 现在新增了独立 `call_spec_v1` JSON 示例文件，供 `import_spec.llm.call_specs` 直接引用。
- 新示例覆盖：
  - Step2 顶层题号范围检测
  - Step3 image-only 单题结构化
  - Step3.5 full 规范化
  - Step4 视觉资产对账
  - Step4 答案表格抽取
- `ModelConfig` 现在改为 `providers: tuple[str, ...>`：
  - `qwen-vl-max` 允许 `dashscope`、`bailian`
  - `qwen3.5-flash` 允许 `dashscope`、`bailian`
- `call_spec_loader.py` 不再要求模型只能绑定单一 provider，而是校验 `call_spec.provider` 是否在该模型允许列表中。

验证：

- 已静态解析 5 个百炼 call spec 示例，`load_and_resolve_call_spec()` 全部通过。
- 结果包括：
  - `step2_question_ranges... -> bailian / qwen-vl-max / submit_question_ranges`
  - `step3_question_json... -> bailian / qwen-vl-max / image_only_question_standardization`
  - `step35_latex_audit... -> bailian / qwen3.5-flash / submit_step3_5_record`
  - `step4_visual_assets... -> bailian / qwen-vl-max / submit_step4_visual_assets`
  - `step4_answer_tables... -> bailian / qwen-vl-max / submit_step4_answer_tables`

### 新增 v12 prompt/schema 合同一致性检查入口

修改范围：

- 新增 `assets/v12_runtime/exam_import_v12/cli/check_contracts.py`
- 修改 `assets/v12_runtime/V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- 之前 v12 只有“文档上分层”，还缺一个可直接运行的检查入口去验证 prompt 文件、标题路径和 tool schema 没有漂移。
- `V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md` 已经把一致性检查列为收敛方向，应该落成实际脚本，而不是继续停留在建议层。
- 随后又新增了独立 `call_specs/*.json` 示例文件，检查入口也需要一并覆盖 call spec 解析链路。

影响：

- `check_contracts.py` 现在会校验：
  - `KNOWN_PROMPTS` 中的 prompt 文件是否存在
  - 运行时代码依赖的 heading path 是否存在
  - `TOOL_SCHEMA_FILES` 中的 alias 是否都能解析到独立 `.schema.json`
  - `step2/question_record/visual_asset/answer_table` 四类核心 tool schema 的顶层 required 字段是否与当前运行约定一致
  - `call_specs/*.json` 是否都能静态解析到 `provider/model/prompt/tool`
- 这不会调用模型，不读真实 source run，只做结构层校验。

验证：

- 已运行 `python.exe assets/v12_runtime/exam_import_v12/cli/check_contracts.py`。
- 结果为：
  - `prompt_ref_count=19`
  - `tool_schema_alias_count=10`
  - `call_spec_count=5`
  - 所有 prompt 文件、heading path、tool schema alias、call spec 示例检查通过。

### 补齐 v12 source_import 的 raw PDF 抽取链路并拆出 Step4.5 sync

修改范围：

- 新增 `assets/v12_runtime/exam_import_v12/sources/page_assets.py`
- 新增 `assets/v12_runtime/exam_import_v12/sources/mineru_extract.py`
- 新增 `assets/v12_runtime/exam_import_v12/sources/source_run_writer.py`
- 新增 `assets/v12_runtime/exam_import_v12/steps/step45_sync.py`
- 修改 `assets/v12_runtime/exam_import_v12/sources/__init__.py`
- 修改 `assets/v12_runtime/exam_import_v12/schemas/import_spec.py`
- 修改 `assets/v12_runtime/exam_import_v12/cli/source_import.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/step4_runtime.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/__init__.py`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- `import_spec_v2` 之前已经允许填写 `paper_pdf`、`answer_pdf`、`mixed_pdf`，但 `source_import.py` 实际只支持 prepared dir，属于契约和实现不一致。
- 这会让 v12 仍然依赖外部先生成 source run，严格说没有完成自己的 source import 重构。
- 同时架构文档中已经把 `Step4.5 sync` 单独列成职责，但代码里仍混在 `step4_assets.py` 调用路径中，目录结构和文档有偏差。

影响：

- `source_import.py` 现在支持两种正式入口：
  - prepared source part 复制导入
  - raw PDF 直接抽取 source run
- raw PDF 抽取支持显式 extractor：
  - `local_pymupdf`
  - `mineru_vlm`
- `schemas/import_spec.py` 新增：
  - `sources.paper_extractor`
  - `sources.answer_extractor`
  - `sources.mixed_extractor`
  - `mineru.*` 配置段
- `sources/page_assets.py` 负责 PDF 页码切分与页面渲染。
- `sources/mineru_extract.py` 负责本地 `PyMuPDF` 抽取和 MinerU VLM 抽取，不依赖 v11 helper。
- `sources/source_run_writer.py` 负责 source part 级别的 staged PDF、复用规则和写盘。
- `step45_sync.py` 现在把 Step4 同步 question bank 的职责单独落成文件；`step4_runtime.py` 通过该包装层执行同步。

验证：

- 已对 `assets/v12_runtime/exam_import_v12/**/*.py` 执行源码级 `compile()` 检查，结果通过。
- 已做 `validate_spec.py` 烟测：新的 `paper_extractor` 和 `mineru` 字段可正常解析并规范化输出。
- 已做 `source_import.py` 本地抽取烟测：对真实 PDF `tmp/import_2019_bailian_qwen35_flash_fast/.../1d748d83-51ad-4c8f-a109-099acd5db89f_origin.pdf` 成功生成 source run，`page_count=4`。
- 已做 `paper_page_range=1-2` 烟测：成功生成 `_split_sources/paper_pages_1_2.pdf`，输出 `page_count=2`，并能继续生成 Step2 `question_packets`。
- 本轮未做真实 MinerU 联调；当前仅验证了配置、代码路径和本地 extractor 路线。

### 补齐 v12 Step5 的真实资产导出链路

修改范围：

- 新增 `assets/v12_runtime/exam_import_v12/render/asset_export.py`
- 修改 `assets/v12_runtime/exam_import_v12/render/asset_resolver.py`
- 修改 `assets/v12_runtime/exam_import_v12/render/__init__.py`
- 修改 `assets/v12_runtime/exam_import_v12/render/mathjax_page.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/step5_render.py`
- 修改 `assets/v12_runtime/exam_import_v12/cli/run_pipeline.py`
- 修改 `assets/v12_runtime/prompts/zh_CN/step5_render.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- 之前 v12 Step5 只会从 `rendered_question_bank_mathjax/<run_id>/assets/` 被动查找 `label.*`，但没有任何正式代码把 source run 里的真实图片/表格导出到该目录。
- 这会导致渲染页虽然能生成，但大量 `<img src>`、`<table src>`、`<chart src>` 最终只显示 `Missing asset`，审阅价值不足。
- source run 的 `ocr_blocks.json` 已经提供了 `path`、`mineru_item.img_path`、`table_body` 等足够字段，适合在 Step5 增加一层窄而明确的资产导出逻辑，不必反向改 Step3/Step4 协议。

影响：

- 新增 `render/asset_export.py`，负责：
  - 扫描 `question_bank.json` 中实际出现的资产占位标签
  - 通过 Step2 `question_packets/`、`answer_packets/` 恢复 `label -> source block` 映射
  - 从 source run 导出真实资产到 `rendered_question_bank_mathjax/<run_id>/assets/`
  - 写出 `assets_manifest.json`
- 表格资产现在按固定优先级处理：
  - 优先导出 `table_body -> <label>.html`
  - 若 source run 同时有表格截图，也一并复制为 `<label>.<suffix>`
- `AssetResolver` 现在按标签类型选扩展名：
  - `table` 优先 `.html`
  - `img/chart` 优先光栅图
- `step5_render.py` 现在在渲染前执行资产导出，并把 `assets_manifest` 与导出统计写入 `summary.json` 和返回 summary。
- `run_pipeline.py` 的 Step5 evidence 现在额外记录：
  - `assets_manifest`
  - `asset_reference_count`
  - `exported_asset_count`
  - `missing_asset_count`
- 渲染页补了表格样式，便于直接审阅结构化表格。

验证：

- 已对 `assets/v12_runtime/exam_import_v12/**/*.py` 再次执行源码级 `compile()` 检查，结果通过。
- 已用真实 source run `tmp/import_2019_bailian_qwen35_flash_fast/runs/source_runs/shanghai_2019_spring_hi_quality_bailian_qwen35_flash_fast/paper` 做 Step5 资产导出烟测：
  - 成功导出 `Q-V01-P01.jpg`
  - 成功导出 `Q-V03-P01.html`
  - 成功导出 `Q-V03-P01.jpg`
  - `assets_manifest.json` 统计为 `asset_reference_count=2`、`exported_asset_count=2`、`missing_asset_count=0`
  - 最终 HTML 中未出现 `Missing asset`
- 已清理本轮临时 smoke 目录 `tmp/v12_render_asset_export_smoke`。

### 实装 v12 的 Step2 端到端链路与 Step4 本地 compact 构建

修改范围：

- 新增 `assets/v12_runtime/exam_import_v12/sources/ocr_blocks.py`
- 新增 `assets/v12_runtime/exam_import_v12/steps/step2_runtime.py`
- 新增 `assets/v12_runtime/exam_import_v12/steps/step4_runtime.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/__init__.py`
- 修改 `assets/v12_runtime/exam_import_v12/cli/run_pipeline.py`
- 修改 `assets/v12_runtime/exam_import_v12/sources/__init__.py`
- 修改 `assets/v12_runtime/exam_import_v12/sources/ocr_blocks.py`
- 修改 `assets/v12_runtime/exam_import_v12/schemas/visual_asset.py`
- 修改 `assets/v12_runtime/exam_import_v12/schemas/__init__.py`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- v12 虽然已有 Step2 本地 builder 和 Step3-Step5 局部函数，但仍缺少真正的 Step2 source_run -> prompt -> qa_alignment -> crop 端到端链路。
- Step4 之前依赖外部先准备好 compact input，实际仍不算被 v12 接管。
- `VisualAssetReview.risks` 的运行时 schema 和 prompt/schema 不一致，真实 Step4 结果会在本地校验阶段误报失败。

影响：

- `sources/ocr_blocks.py` 现在负责从 `ocr_blocks.json` 生成 layout items、标注页图片和 page packets。
- `step2_runtime.py` 现在负责：
  - 读取 source run
  - 调用 Step2 prompt
  - 写 `step2_pure_paper_ranges.json` / `step2_pure_answer_ranges.json` / `step2_mixed_ranges.json`
  - 构建 `qa_alignment.json` 和 `pipeline_summary.json`
  - 生成实际 Step3 crop 图片与 `crops_manifest.json`
- `run_pipeline.py` 现在在 `skip_step2=false` 时会真正执行 Step2，而不是只假定 Step2 产物已存在。
- `step4_runtime.py` 现在负责从：
  - `qa_alignment.json`
  - Step2 packets
  - Step3/Step3.5 question bank
  本地构建 Step4 的逐页 compact payload，并聚合多页 Step4 结果后再同步回题库。
- `VisualAssetReview.risks` 已改为 `label / severity / reason`，与 Step4 prompt 和 tool schema 一致。

验证：

- 已对 `assets/v12_runtime/exam_import_v12/**/*.py` 执行源码级 `compile()` 检查，结果通过。
- 已做 `build_step2_messages()` 烟测，通过。
- 已做 `write_step2_crops()` 烟测，通过。
- 已做 `run_step2()` 烟测：使用现成 source run 和 fake tool-call 响应，能够写出 `qa_alignment.json`、`pipeline_summary.json`、`crops_manifest.json`。
- 已做 `build_step4_inputs()` 烟测，通过。
- 已做 `run_step4()` 烟测：使用 fake tool-call 响应，能够写出 Step4 compact input、`visual_asset_assignment.json` 和同步后的 `question_bank.json`。
- 已做 `run_pipeline()` 的 orchestration 烟测：通过 monkeypatch 各步调用点，确认 Step2-Step5 编排、evidence report 和 render 输出路径可连通。

### 实装 v12 的 prepared-dir source_import

修改范围：

- 修改 `assets/v12_runtime/exam_import_v12/schemas/import_spec.py`
- 新增 `assets/v12_runtime/exam_import_v12/cli/source_import.py`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- v12 不能继续只有 `source_import.py` 空壳，否则整个 runtime 仍缺正式的 source run 入口。
- 但当前又没有完成原始 PDF 的 OCR 抽取链路；最稳的推进方式是先明确支持“导入现成 prepared source part 目录”，把已有真实产物正式纳入 v12，而不是假装 raw PDF 已可直接跑通。
- 这也符合“不依赖 v11 依赖”的约束：v12 可以消费外部准备好的 `ocr_blocks.json + pages/`，但不 import v11 helper。

影响：

- `SourceConfig` 新增：
  - `paper_prepared_dir`
  - `answer_prepared_dir`
  - `mixed_prepared_dir`
- `source_import.py` 现在支持把 prepared source part 拷贝到：
  - `source_runs/<run_id>/paper/`
  - `source_runs/<run_id>/answer/`
  - `source_runs/<run_id>/mixed/`
- `answer_patch` 模式下，source import 会保留既有 `paper/`，只处理 `answer/`。
- 如果只提供原始 PDF 而不提供 prepared dir，当前会直接失败，不伪装为已支持 OCR 抽取。

验证：

- 已做 `ImportSpec.from_dict()` 烟测，prepared-dir 字段可正常解析。
- 已做 `run_source_import()` 烟测：最小 prepared `paper/` 目录可被复制到 `source_runs/<run_id>/paper/`，并生成 `source_item.json` 与 source import summary。

### 实装 v12 的 artifact-driven run_pipeline 入口

修改范围：

- 修改 `assets/v12_runtime/exam_import_v12/schemas/import_spec.py`
- 新增 `assets/v12_runtime/exam_import_v12/cli/run_pipeline.py`
- 修改 `assets/v12_runtime/exam_import_v12/cli/answer_patch.py`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- v12 之前只有 Step3-Step5 的局部函数，没有真正的 CLI 编排入口，导致“模块能 import，但流程不能跑”。
- 单个 `llm.call_spec_path` 不足以支撑 Step3、Step3.5、Step4 这类不同 prompt 和 tool schema 的多步骤流程，需要按步骤映射 call spec。
- answer patch 入口也不应继续是纯占位，应至少完成 mode preflight 并复用统一编排入口。

影响：

- `LLMConfig` 新增 `call_specs` 映射，并提供 `resolve_call_spec_path(step_name)`。
- `run_pipeline.py` 现在可基于现有 v12 产物执行 Step3、Step3.5、Step4、Step5：
  - 读取 `qa_alignment.json`
  - 读取 Step3 crops
  - 读取 `question_bank.json`
  - 读取 Step4 compact input JSON
  - 写出 evidence report
- `run_pipeline.py` 当前是 artifact-driven 入口：缺少必需上游产物时直接失败，不做静默 fallback。
- `answer_patch.py` 现在会校验 `input_mode=answer_patch`，再复用 `run_pipeline.py`。

验证：

- 已对 `assets/v12_runtime/exam_import_v12/**/*.py` 再次执行源码级 `compile()` 检查，结果通过。
- 已做 `ImportSpec.llm.call_specs` 烟测，`resolve_call_spec_path()` 可正常返回按步骤映射的路径。
- 已做 `run_pipeline()` 的 render-only 烟测：在仅存在 `question_bank.json`、关闭 Step3/Step4 的最小场景下，可生成 render 输出和 evidence report。

### 把 v12 tool schema 真正外置成独立 JSON 文件

修改范围：

- 新增 `assets/v12_runtime/tool_schemas/step2_question_ranges.schema.json`
- 新增 `assets/v12_runtime/tool_schemas/question_record.schema.json`
- 新增 `assets/v12_runtime/tool_schemas/visual_asset_review.schema.json`
- 新增 `assets/v12_runtime/tool_schemas/answer_table_review.schema.json`
- 新增 `assets/v12_runtime/exam_import_v12/schemas/question_ranges.py`
- 修改 `assets/v12_runtime/exam_import_v12/core/paths.py`
- 重写 `assets/v12_runtime/exam_import_v12/llm/tool_schemas.py`
- 修改 `assets/v12_runtime/exam_import_v12/schemas/__init__.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/step4_assets.py`
- 修改 `assets/v12_runtime/V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- 当前 v12 虽然已经有 prompt/schema 分层文档，但运行时的 tool schema 还藏在 Python 大字典里，结构上还不够稳。
- 用户明确建议把 prompt 和 JSON schema 分开放置；如果 schema 继续写死在代码里，后续仍容易出现 prompt、tool schema、运行时 schema 三层漂移。
- Step2 顶层题号范围检测虽然已有 prompt 契约，但还没有对应的运行时 schema。

影响：

- v12 tool schema 现在独立落盘到 `assets/v12_runtime/tool_schemas/*.schema.json`。
- `exam_import_v12/llm/tool_schemas.py` 改成轻量 alias + 文件加载器，不再保存 schema 主体。
- 新增 `schemas/question_ranges.py`，对 Step2 `submit_question_ranges` 工具输出做本地强校验。
- `step4_assets.write_step4_outputs()` 现在会同步写回 `question_bank.jsonl`，避免 Step4 后 question bank 两个索引文件不一致。
- v12 架构文档、实现说明和 prompt/schema 分层文档已同步改成新的落盘结构。

验证：

- 已对新建的 4 个 `tool_schemas/*.schema.json` 做 JSON 解析校验。
- 已校验 `resolve_tool_schema()` 能从外置 schema 文件读取 `submit_question_ranges`、`QuestionRecord`、`submit_step4_visual_assets`、`submit_step4_answer_tables`。
- 已校验 `QuestionRangesResult.from_dict()` 能通过最小样例。
- 已校验 Step4 写回后同时生成 `question_bank.json` 和 `question_bank.jsonl`。

### 补齐 v12 prompt 与 JSON Schema 分层结构文档入口

修改范围：

- 新增 `assets/v12_runtime/V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`
- 修改 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`
- 修改 `assets/v12_runtime/prompts/zh_CN/README.md`

修改原因：

- 当前 v12 已经同时存在 prompt 文档、运行时 schema 和 tool schema，但如果没有单独的结构约束文档，后续很容易再次出现“prompt 改了，tool schema 没改”或“schema 改了，步骤代码还在读旧字段”的漂移。
- 用户建议把 prompt 和 JSON schema 的放置结构单独成文，这个方向是对的，应该让这部分规则脱离单个步骤 prompt，成为 v12 的统一约束。

影响：

- 新增 `V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`，明确四层分工：
  - `prompts/zh_CN/*.md`
  - `exam_import_v12/schemas/*.py`
  - `exam_import_v12/llm/tool_schemas.py`
  - `exam_import_v12/steps/*.py`
- 明确稳定引用规则：`prompt_ref`、`tool_name`、`tool_schema_ref`。
- 明确当前 `tool_schemas.py` 仍是过渡实现，后续应继续朝独立 schema 文件和一致性检查脚本收敛。
- 在 v12 主架构文档、实现说明和 prompt README 中补齐该文档入口，避免新文档成为孤立说明。

验证：

- 已检查 `V12_PROMPT_SCHEMA_LAYOUT.zh-CN.md`、`V12_REFACTOR_ARCHITECTURE.zh-CN.md`、`V12_IMPLEMENTATION_NOTES.zh-CN.md`、`prompts/zh_CN/README.md` 的交叉引用关系。
- 本次仅修改文档，未改 Python 运行时代码，未跑 pipeline。

### 继续落地 v12 Step3-Step5 业务层与本地渲染

修改范围：

- 新增 `assets/v12_runtime/exam_import_v12/core/io.py`
- 新增 `assets/v12_runtime/exam_import_v12/llm/response_parser.py`
- 新增 `assets/v12_runtime/exam_import_v12/llm/tool_schemas.py`
- 新增 `assets/v12_runtime/exam_import_v12/schemas/answer_table.py`
- 新增 `assets/v12_runtime/exam_import_v12/steps/step3_question_json.py`
- 新增 `assets/v12_runtime/exam_import_v12/steps/step35_normalize.py`
- 新增 `assets/v12_runtime/exam_import_v12/steps/step4_assets.py`
- 新增 `assets/v12_runtime/exam_import_v12/steps/step5_render.py`
- 新增 `assets/v12_runtime/exam_import_v12/render/asset_resolver.py`
- 新增 `assets/v12_runtime/exam_import_v12/render/markdown_renderer.py`
- 新增 `assets/v12_runtime/exam_import_v12/render/mathjax_page.py`
- 修改 `assets/v12_runtime/exam_import_v12/prompts/loader.py`
- 修改 `assets/v12_runtime/exam_import_v12/steps/__init__.py`
- 修改 `assets/v12_runtime/exam_import_v12/schemas/__init__.py`
- 修改 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`

修改原因：

- v12 只有基础骨架还不够，必须开始具备 Step3、Step3.5、Step4、Step5 的实际业务落点。
- Step4 prompt 文档存在重复 `system prompt` / `user prompt` 标题，原 PromptLoader 无法按标题路径提取子节。
- Step5 需要按照前序文档要求改为解析 Markdown，并使用开源库而不是手写 Markdown 解析。

影响：

- 新增统一 JSON I/O 层。
- 新增 LLM tool call 响应解析层。
- 新增临时 tool schema 注册表，用于驱动 Step3、Step3.5、Step4 的 tool calling 请求。
- 新增答案表抽取 schema。
- Step3 已具备 image-only prompt message 构造、tool calling request 组装、响应解析和 question bank 写入能力。
- Step3.5 已具备最小本地审计、full normalize message 构造、响应解析和结果写回能力。
- Step4 已具备视觉资产审查调用入口、答案表抽取调用入口，以及将结果同步回 question bank 的本地逻辑。
- Step5 已改为使用开源 `markdown` 库渲染 Markdown 字段，并通过受控 adapter 渲染 `<blank>`、`<choice_blank>`、`<img src>`、`<table src>`、`<chart src>`、`<options no>`。
- PromptLoader 新增 fenced block 提取和按标题路径取子节能力，解决 Step4 文档重复标题问题。
- 本轮实现细节单独记录到 `V12_IMPLEMENTATION_NOTES.zh-CN.md`，没有把实现取舍混入架构文档。

验证：

- 已对 `assets/v12_runtime/exam_import_v12/**/*.py` 执行源码级 `compile()` 检查，结果通过。
- 已运行 Step3 image-only message 构造烟测，通过。
- 已运行 Step3.5 审计与消息构造烟测，通过。
- 已运行 Step4 标题路径抽取、资产同步和答案表合并烟测，通过。
- 已运行 Step5 HTML 渲染烟测，能够生成 `index.html`。
- 已清理测试产生的 `__pycache__`。
- 当前仍未实现 source import、pipeline orchestration 和真实 LLM 端到端执行 CLI。

## 2026-06-04

### 落地 v12 独立基础包与配置层

修改范围：

- 新增 `assets/v12_runtime/exam_import_v12/`
- 新增 `assets/v12_runtime/V12_IMPLEMENTATION_NOTES.zh-CN.md`
- 修改 `assets/v12_runtime/prompts/zh_CN/step2_crop_algorithm.md`
- 修改 `assets/v12_runtime/prompts/zh_CN/step5_render.md`

修改原因：

- 用户目标已明确为真正完成 v12 重构，且 v12 不应依赖 v11 依赖或 v11 helper。
- 现状只有 v12 prompt 文档，没有任何可导入代码，无法承载后续 Step2-Step5 实现。
- v12 prompt 中还残留两处旧结构冲突：`step2_crop_algorithm.md` 仍读 `question_groups.json`，`step5_render.md` 仍读旧版 `*_latex` 字段。

影响：

- 新增 `exam_import_v12` 独立包骨架，使用标准库实现，不 import `v11_runtime` Python 模块。
- 落地 `core/`、`prompts/`、`schemas/`、`llm/`、`cli/validate_spec.py` 基础层。
- 落地 `steps/step2_layout.py` 和 `steps/step2_crop.py`，先把不依赖大模型的 Step2 本地层迁入 v12。
- 落地 `schemas/call_spec.py`、`schemas/import_spec.py`、`schemas/qa_alignment.py`、`schemas/question_record.py`、`schemas/visual_asset.py`、`schemas/pipeline_summary.py`。
- 落地 `llm/providers.py`、`llm/model_configs.py`、`llm/call_spec_loader.py`、`llm/request_builder.py`、`llm/client.py`、`llm/token_budget.py`。
- `PromptLoader` 支持 prompt 名称、相对路径和 `#标题` section 引用，并使用字面 `{name}` 替换变量。
- `cli/validate_spec.py` 已可直接运行并输出规范化后的 v12 import spec；`source_import.py`、`run_pipeline.py`、`answer_patch.py` 当前显式失败，避免假入口。
- 新增实现细节文档，单独记录本轮加入的 provider 注册表、token 估算、prompt 解析规则和未实现边界。
- 修正 v12 prompt 中 `step2_crop_algorithm.md` 和 `step5_render.md` 的旧字段残留，使其与 `qa_alignment_v2` 和 Markdown 字段结构一致。

验证：

- 已对 `assets/v12_runtime/exam_import_v12/**/*.py` 执行源码级 `compile()` 检查，结果通过。
- 已运行 prompt loader section 烟测：`step3_question_json#image-only 模式 system prompt` 读取通过。
- 已运行 `load_and_resolve_call_spec()` 烟测，`dashscope + qwen3.5-flash + step3_question_json` 解析通过。
- 已运行 `validate_spec.py` CLI 烟测，临时 `import_spec_v2` 可正常输出规范化 JSON。
- 已运行 `build_qa_alignment_document()` 烟测，`pure_paper` 模式可生成 `qa_alignment_v2` 和 `pipeline_summary`。
- 已运行 `crop_islands_for_question_page()` 烟测，跨栏样例可拆成两个 crop island。
- 已扫描 v12 prompt 目录，不再残留 `step5_render.md` 中的旧版 `question_type` / `*_latex` 读取契约。
- 未开始实现 Step2-Step5 业务逻辑；当前完成的是 v12 独立基础层。

## 2026-06-04

### 输出 v12 runtime 重构整体架构文档

修改范围：

- 新增 `assets/v12_runtime/V12_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `assets/v12_runtime/prompts/zh_CN/README.md`

修改原因：

- v12 runtime 需要一份整体架构文档，承接已审定的 Step2-Step5 prompt、`qa_alignment_v2` 四模式主表、providers/model_configs/call_spec 三层分离和 Markdown 渲染方向。
- v12 不需要旧步骤兼容，因此架构文档应明确不保留旧 mode alias、旧扁平字段和 `question_groups.json` 旁路。
- v12 prompt README 仍写成 v11，需要同步为 v12 的 prompt source of truth 说明。

影响：

- 新文档定义 v12 架构目标、非目标、目标目录结构、总体数据流、四种输入模式、核心产物、LLM 配置架构、prompt/schema 关系、Step2-Step5 职责边界、证据输出、测试策略和迁移顺序。
- 明确 `qa_alignment.json` 是 Step2 后唯一主链路表。
- 明确每次模型调用读取显式 `call_spec.json`，再合并 `model_configs.py` 和 `providers.py`。
- 仅修改文档；当前 Python 运行逻辑不变。

验证：

- 已检查 v12 架构文档和 README 中不再残留 v11 prompt README 标题。
- 未运行 pipeline；本次仅新增/修改文档。

## 2026-06-04

### 输出 Step2 qa_alignment 四模式契约

修改范围：

- 新增 `assets/v11_runtime/prompts/zh_CN/step2_qa_alignment_contract.md`
- 修改 `assets/v11_runtime/prompts/zh_CN/step_pipeline_processing_flow.md`
- 修改 `assets/v11_runtime/prompts/zh_CN/README.md`

修改原因：

- `qa_alignment.json` 需要同时适配 `pure_paper`、`mixed`、`paper_plus_answer`、后导入 `answer` 四种模式。
- `question_groups.json` 与 `qa_alignment.json` 存在职责重叠风险，需要明确 `qa_alignment.json` 是唯一主链路表，不再设计独立题面分组旁路。
- 用户确认不需要加入对旧步骤的兼容，因此契约只保留新流程规范字段。

影响：

- 新文档定义 `alignment_mode` 四个规范值：`pure_paper`、`mixed`、`paper_plus_answer`、`answer_patch`。
- 定义 `qa_alignment_v2` 顶层结构、每题行结构、四种模式的写入规则和 JSON Schema。
- 移除 `runtime_input_mode`、旧扁平行字段和 mode alias 映射。
- 总流程文档改为 Step3、Step4、Step5 只以 `qa_alignment.json` 为权威来源，不再输出或读取 `question_groups.json`。
- 仅修改 prompt 目录文档；当前 Python 运行逻辑不变。

验证：

- 已校验新增和修改文档中的 JSON 代码块可被 JSON 解析。
- 未运行 pipeline；本次仅新增/修改文档。

## 2026-06-04

### 输出 Step2 裁剪图 crop island 算法设计

修改范围：

- 新增 `assets/v11_runtime/prompts/zh_CN/step2_crop_algorithm.md`
- 修改 `assets/v11_runtime/prompts/zh_CN/README.md`
- 修改 `assets/v11_runtime/prompts/zh_CN/step_pipeline_processing_flow.md`

修改原因：

- 同页跨栏题目如果继续对同题同页所有 bbox 做单个 union，会把两栏之间无关区域裁入 Step3 输入图。
- 当前只需要解决这个裁剪污染问题，不调整 Step2 范围检测、分栏识别或 Step4 资产归属逻辑。

影响：

- 新文档定义 Step2 裁剪阶段的基础规则：按 label 聚合 bbox、按 stream/page 分组、bbox 缩放、共享 bbox 拆分、visual_labels 参与裁剪。
- 新增 crop island 算法：同页同题先按空间连通性聚类，每个 island 单独 union 并输出多张有序裁剪图。
- 明确 Step3 输入图片顺序即阅读顺序，同一道题可有多张题面裁剪图。
- 明确该算法不改变 `start_label/end_label`、`visual_labels`、Step4 资产占位对账或 Step3 字段结构。
- README 和总流程文档增加该算法文档入口。
- 仅修改 prompt 目录文档；当前 Python 运行逻辑不变。

验证：

- 未运行 pipeline；本次仅新增/修改文档。

## 2026-06-04

### 输出 Step2-Step5 处理方式与流转设计文档

修改范围：

- 新增 `assets/v11_runtime/prompts/zh_CN/step_pipeline_processing_flow.md`
- 修改 `assets/v11_runtime/prompts/zh_CN/README.md`

修改原因：

- 已经为 Step4 视觉资产占位对账单独输出算法文档，其他步骤也需要把原先处理方式、上下游流转、失败边界和当前重构字段方向落成文档，便于后续迁移运行时代码时对照。

影响：

- 新文档记录 Step2 顶层题号范围检测、Step3 单题结构化、Step3.5 full 规范化、Step4 占位对账与答案表抽取、Step5 渲染的端到端流转。
- 明确 Step2 只切范围、Step3 负责字段整理、Step3.5 只做格式规范化、Step4 不重写正文、Step5 不调用大模型。
- 明确各步骤主要输入、输出、失败边界和向下一步传递的数据。
- README 增加该流程文档入口。
- 仅修改 prompt 目录文档；当前 Python 运行逻辑不变。

验证：

- 未运行 pipeline；本次仅新增/修改文档。

## 2026-06-04

### 输出 Step4 视觉资产占位对账算法设计

修改范围：

- 新增 `assets/v11_runtime/prompts/zh_CN/step4_asset_placeholder_algorithm.md`
- 修改 `assets/v11_runtime/prompts/zh_CN/step4_assets.md`
- 修改 `assets/v11_runtime/prompts/zh_CN/README.md`

修改原因：

- Step4 需要明确算法层职责：所有图片、表格、图表必须由本地代码全量枚举，模型只对已枚举资产做占位对账。
- 对范围外图片的处理需要固定：如果可确认属于某题某字段，不扩大 Step2 范围，而是追加到该字段最后。
- 用户明确没有“一图多归属”；同一图片如果在 Step3 多处出现，应视为重复或误放占位，由 Step4 选择唯一最终位置。

影响：

- 新文档定义了 Step4 资产索引、Step2 范围索引、Step3 占位索引和候选题号索引。
- 明确 `in_range`、`visual_label`、`outside_nearest`、`orphan` 候选关系。
- 明确范围外图片使用 `append_to_field_end` 或 `append_to_option_end`。
- 明确每个资产只能输出一次、只能有一个最终题号、一个最终字段和一个最终占位。
- 明确禁止 `placements[]`。
- `step4_assets.md` 的视觉资产 schema 增加 `insert_position`，并补充全量遍历、单图唯一归属和范围外末尾追加规则。
- README 增加该算法设计文档入口。
- 仅修改 prompt/设计文档；当前 Python 运行逻辑和工具 schema 仍需后续同步。

验证：

- 未运行 pipeline；本次仅新增/修改 prompt 目录文档。

## 2026-06-04

### 收窄 Step4 视觉任务为 Step3 资产占位对账

修改范围：

- 修改 `assets/v11_runtime/prompts/zh_CN/step4_assets.md`

修改原因：

- Step3 已负责在 `stem_markdown`、`options_markdown`、`answer_markdown`、`analysis_markdown` 中放置 `<img>`、`<table>`、`<chart>` 占位。
- Step4 不应再全面重做资产归属，只需要校验 Step3 占位与版面资产是否一致，并处理两类边界情况：资产属于某字段但 Step3 未放占位、资产不属于某字段但 Step3 已放占位。

影响：

- Step4 视觉任务描述从“资产归属”调整为“资产占位对账”。
- 输入资产上下文增加 `step3_placeholder_refs`，用于记录 Step3 当前已有占位位置。
- 输出 schema 增加 `placeholder_status`、`action`、`source_field`。
- 增加 `matched`、`belongs_but_missing_placeholder`、`placeholder_but_not_belong`、`wrong_field`、`wrong_tag`、`no_placeholder_needed`、`uncertain` 状态。
- 增加 `keep_existing`、`add_placeholder`、`remove_placeholder`、`move_placeholder`、`ignore_asset`、`review_required` 动作。
- 明确同步阶段只按 Step4 对账结果做新增、删除或移动占位建议，不重写 Step3 正文。

验证：

- 未运行 pipeline；本次仅修改 prompt 文档。

## 2026-06-04

### 对齐 Step3.5 prompt 到 full 全量规范化模式

修改范围：

- 修改 `assets/v11_runtime/prompts/zh_CN/step35_latex_audit.md`

修改原因：

- Step3.5 应与之前验证过的 full 全量修复模式对齐，不应只按脚本 `audit_findings` 局部修复。
- 新 Step3 schema 已改为 Markdown 字段，因此需要把 full 模式的“完整审计所有文字字段、即使 audit_findings 为空也处理、before/after audit 与内容指纹保护”语义迁移到 `stem_markdown`、`options_markdown[].options[].content_markdown`、`answer_markdown`、`analysis_markdown`。

影响：

- Step3.5 prompt 标题和任务边界改为 full Markdown/LaTeX normalize。
- 明确 `audit_findings` 只是提示，不是修复范围上限。
- 明确必须完整审计所有 Step3 Markdown 文字字段。
- 补齐 full 模式中的 LaTeX、HTML 实体、`<blank>`、`<choice_blank>`、`\left/\right`、数学符号保真和 issues 保留规则。
- 仅修改 prompt 文档；当前 Python 运行逻辑仍需后续同步迁移到新 Step3 Markdown schema。

验证：

- 已对照 `step3_5_full_latex_normalize.py` 的 full 模式 prompt 边界。
- 未运行 pipeline；本次无运行代码变更。

## 2026-06-04

### 对齐 Step3.5 与 Step4 prompt 到 Step2/Step3 审定结构

修改范围：

- 修改 `assets/v11_runtime/prompts/zh_CN/step35_latex_audit.md`
- 修改 `assets/v11_runtime/prompts/zh_CN/step4_assets.md`

修改原因：

- Step3.5 和 Step4 的 prompt 文档上一次仍按当前旧运行时 schema 描述，保留了 `question_type`、`stem_latex`、`options_latex`、`rubric_latex` 等旧字段，与已审定的 Step3 `image_only_question_standardization_v1` 结构不兼容。
- Step4 仍使用独立 `mode` 和旧资产 role 语义，没有明确对齐 Step2 的 `range_mode` 与 Step3 的 Markdown 字段/占位标签。

影响：

- Step3.5 输出 schema 改为与 Step3 完全一致：`schema_version`、`question_no`、`stem_markdown`、`options_markdown`、`answer_markdown`、`analysis_markdown`、`issues`。
- Step3.5 明确禁止输出旧版 `question_type`、`stem_latex`、`options_latex`、`answer_latex`、`analysis_latex`、`rubric_latex`。
- Step4 输入改为沿用 Step2 `range_mode`，并显式携带 `step2_question_ranges`、`step3_records` 和资产候选上下文。
- Step4 视觉资产输出改为指向 Step3 目标字段：`stem_markdown`、`options_markdown`、`answer_markdown`、`analysis_markdown`、`none`，并输出可回填的 `<img>`、`<table>`、`<chart>` 占位。
- Step4 答案页表格抽取输出改为 `answer_markdown` 数组，不再使用旧版 `answer_text`；评分表和解答表归入 `analysis_markdown` 语义。
- 仅修改 prompt 文档；当前 Python 运行逻辑仍需后续同步迁移到新 schema 后才能直接执行这些 prompt。

验证：

- 已检查 Step3.5 文档不再残留旧版输出字段。
- 已检查 Step4 文档包含 `range_mode`、Step3 Markdown 字段、Step3 占位标签和百炼强制 tool call 约束。
- 未运行 pipeline；本次无运行代码变更。

## 2026-06-04

### 改写 Step3.5 与 Step4 输出格式为百炼 Function Calling JSON Schema

修改范围：

- 修改 `assets/v11_runtime/prompts/zh_CN/step35_latex_audit.md`
- 修改 `assets/v11_runtime/prompts/zh_CN/step4_assets.md`

修改原因：

- Step2 prompt 已改为百炼 Function Calling schema 文档格式，Step3.5 和 Step4 需要保持同样的 prompt 文档结构，明确工具定义、强制调用和本地校验边界。

影响：

- `step35_latex_audit.md` 现在记录 `submit_step3_5_record` 的百炼工具定义和 `Step3Record` 参数 schema。
- `step4_assets.md` 现在记录 `submit_step4_visual_assets` 与 `submit_step4_answer_tables` 的百炼工具定义。
- 外置文档统一记录 `enable_thinking=false`、强制 `tool_choice`、`parallel_tool_calls=false`、从 `message.tool_calls[0].function.arguments` 读取结果和本地二次校验。
- 仅修改 prompt 文档，不修改 Python 运行逻辑、CLI 或产物布局。

验证：

- 已对照 `step3_5_question_json_audit_fix.py` 中 `STEP3_RECORD_TOOL_SCHEMA`。
- 已对照 `step4_asset_distribution.py` 中 `VISUAL_ASSIGNMENT_TOOL_SCHEMA` 和 `ANSWER_TABLE_TOOL_SCHEMA`。
- 未运行 pipeline；本次无运行代码变更。

## 2026-06-04

### 改写 Step2 输出格式为百炼 Function Calling JSON Schema

修改范围：

- 修改 `assets/v11_runtime/prompts/zh_CN/step2_layout.md`

修改原因：

- Step2 prompt 已改为 `range_mode` 输入语义，输出格式也需要从普通 JSON 示例改为百炼 Function Calling 可直接使用的 `tools[].function.parameters` JSON Schema。

影响：

- 外置 prompt 文档现在记录 `submit_question_ranges` 的百炼工具定义、强制 `tool_choice`、`enable_thinking=false`、`parallel_tool_calls=false` 和本地校验约束。
- 仅修改 prompt 文档，不修改 Python 运行逻辑、CLI 或产物布局。

验证：

- 已对照 `step2_layout.py` 中 `RANGE_DETECTOR_TOOL_SCHEMA` 的字段：`question_ranges`、`noise_blocks`、`risks`。
- 未运行 pipeline；本次无运行代码变更。

## 2026-06-04

### 明确每次 LLM 调用读取显式 call_spec.json

修改范围：

- 修改 `V11_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `FUNCTIONAL_SPEC.zh-CN.md`

修改原因：

- 单次调用配置不应作为 Python 内置 profile 隐式选择；调用时应输入一个明确 JSON，便于审计、复现和按模型/厂商能力失败暴露。

影响：

- 将 `call_profiles.py` 表述改为 `schemas/call_spec.py` / `llm/call_spec_loader.py`。
- 明确每次 LLM 调用读取并校验显式 `call_spec.json`。
- 在功能 SPEC 中加入最小 `call_spec.json` 示例。
- 仅更新文档，不修改运行代码、CLI 或产物布局。

验证：

- 已核对架构文档和功能 SPEC 均包含显式 `call_spec.json` 约束。
- 未运行 pipeline；本次无运行代码变更。

## 2026-06-04

### 细化 LLM provider、model config 与 call spec 边界

修改范围：

- 修改 `V11_REFACTOR_ARCHITECTURE.zh-CN.md`
- 修改 `FUNCTIONAL_SPEC.zh-CN.md`

修改原因：

- provider 厂商接入规则、具体模型静态配置、每次 Step 调用配置如果混在一起，后续会难以处理不同厂商 endpoint、thinking 参数、结构化输出能力和单次调用工具 schema。

影响：

- 明确三层分离：`llm/providers.py` 保存厂商接入与能力；`llm/model_configs.py` 保存模型静态能力；`schemas/call_spec.py` 与 `llm/call_spec_loader.py` 负责显式单次调用配置。
- 仅更新架构和功能 SPEC，不修改运行代码、CLI 或产物布局。

验证：

- 已对照当前 `common_llm.py` 中 provider endpoint/token/thinking 逻辑和 Step2-Step4 的 structured output 调用方式梳理边界。
- 未运行 pipeline；本次无运行代码变更。

## 2026-06-04

### 新增图片并行 Step3 到渲染端到端探测脚本

修改范围：

- 新增 `assets/v11_runtime/probe_image_parallel_step3_render.py`
- 后续补充 `--reuse-parsed-tool-calls`，用于显式复用已成功解析的 `parsed_tool_calls.json`，只重跑 Step3.5、Step4、Step5。
- 后续补充 mixed 解析卷规则：当 `source_mode=mixed` 时，允许从同一批 `paper_pages` 中读取题面、参考答案、分析和解答，并允许 `source_answer_labels` 引用 paper 标签。
- 后续修复 probe 子进程输出捕获编码，`run_command()` 固定使用 UTF-8 并以替换方式处理异常字节，避免 Windows 默认 GBK 解码导致后续步骤中断。
- 后续将 probe 的 Step3.5 接入改为默认 `--step3-5-mode full`，调用 `step3_5_full_latex_normalize.py` 对每道题做全量 LaTeX/占位符规范化；保留 `--step3-5-mode audit` 作为旧审计修复路径。
- 后续补充 `--image-kind asset_marked`，从原始页图生成只标注图片/表格资产的页面图。
- 后续补充 `--prompt-compact-mode asset_labels_only`，模型提示词中的紧凑版面包只保留每页资产标签列表，不给 OCR 文本、普通文本块标签或 bbox。

修改原因：

- 需要验证“传入整页图片，由模型一次响应返回多次 tool call，每道题一个题库 JSON，然后接 Step3.5、Step4、Step5 渲染”的实验路径。
- 该路径不同于正式 pipeline：它绕过 Step2 范围检测，直接由多模态模型做整页转写与题库结构化。

影响：

- 新脚本只写入独立 probe 输出目录，默认在 `assets/v11_runtime/tmp/image_parallel_step3_render_probe` 下。
- 每个 tool call 使用 `submit_image_question_record`，工具参数包含标准 Step3Record 和源标签数组；最终 question bank 只保存标准 Step3Record。
- 支持 `--image-kind annotated/raw/both`。`both` 会同时传入标注图和原图；若 source run 含答案页，也会按同一规则传入答案/解析图片。
- `asset_marked` 图像模式只改变 probe 发给 Step3 图像模型的页面图，不替换正式 Step2 标注图和资产裁剪产物。
- `asset_labels_only` 只改变 Step3 prompt 中的 compact JSON；probe 内部仍保留完整 packet 标签映射，以便生成兼容 Step3.5、Step4、Step5 的临时 Step2 产物。
- `source_mode=mixed` 只影响 probe prompt 和 probe 自建 Step2 兼容产物中的标签过滤；正式 Step2/Step3/Step3.5/Step4/Step5 入口仍不变。
- Step3.5 full 模式只改变 probe 串联方式，不改变正式 Step3.5 CLI；full 模式会把所有题发送给文本模型，不只处理 flagged 题。
- 不修改正式 Step2/Step3/Step3.5/Step4/Step5 入口行为。

验证：

- 已通过 `python.exe -m py_compile assets/v11_runtime/probe_image_parallel_step3_render.py`。
- 已通过 `python.exe assets/v11_runtime/probe_image_parallel_step3_render.py --help`。
- 已用百炼 `qwen3-vl-plus` 对 `tmp/import_runs/qingpu_2022_sep_page1/source_runs/qingpu_2022_sep_quality_page1` 做 annotated-only 单页探测：一次响应返回 12 个 `submit_image_question_record` tool calls，生成 12 条题库记录，`qb_success_count=12`，`qb_error_count=0`，渲染入口生成成功。
- annotated-only 复跑时，同模型同参数返回 12 个 tool calls，但 `index=4`、`index=9` 的 tool call `function.name` 为空，且参数形态不符合 schema；脚本按错误中止，未加入静默修复或 fallback。
- 使用 `--reuse-parsed-tool-calls` 显式复用第一次成功解析的 12 个 tool calls 后，Step3.5 真实审计 `question_count=12`，`flagged_question_count=1`，`before_finding_count=1`，`after_finding_count=0`；该修复因 `content_fingerprint_changed` 被拒绝写回。Step4 识别资产 `asset_count=2`、`assigned_count=2`、`risk_count=2`，同步题库 `changed_question_count=12`；Step5 渲染入口存在。
- 按“传入所有图片”的样例用 `--image-kind both` 重新实测，输出目录为 `tmp/probe_image_parallel_step3_render_all_images/qingpu_2022_sep_quality_page1/image_parallel_step3`：一次响应返回 12 个合法 tool calls，坏 tool 名称数为 0，生成 12 道题；由于该 source run 是 `paper_only`、没有答案页，12 题均记录 `answer_missing`，其中 11 题为 error、1 题为 warning，因此 `qb_success_count=1`、`qb_error_count=11`。Step3.5 审计 `question_count=12`、`flagged_question_count=0`；Step4 资产 `asset_count=2`、`assigned_count=2`、`risk_count=2`，同步 `changed_question_count=12`；Step5 渲染入口：`tmp/probe_image_parallel_step3_render_all_images/qingpu_2022_sep_quality_page1/image_parallel_step3/rendered_question_bank_mathjax/qingpu_2022_sep_quality_page1/index.html`。
- 使用 `qwen3.6-plus` 按同一“传入所有图片”样例实测，输出目录为 `tmp/probe_image_parallel_step3_render_qwen36/qingpu_2022_sep_quality_page1/image_parallel_step3`：一次响应返回 12 个合法 tool calls，坏 tool 名称数为 0，生成 12 道题，`qb_success_count=12`、`qb_error_count=0`。该 source run 无答案页，模型对 12 题均记录 `answer_missing` 且 severity 为 warning，`answer_latex` 和 `analysis_latex` 均为空，未自行补答案。Step3.5 审计 `question_count=12`、`flagged_question_count=0`；Step4 资产 `asset_count=2`、`assigned_count=2`、`risk_count=2`，同步 `changed_question_count=12`；Step5 渲染入口：`tmp/probe_image_parallel_step3_render_qwen36/qingpu_2022_sep_quality_page1/image_parallel_step3/rendered_question_bank_mathjax/qingpu_2022_sep_quality_page1/index.html`。
- 使用 `qwen3.6-plus` 测试 `2023年春考 解析卷.pdf`，先以 `v11_source_import.py --mode mixed` 生成 source run：`tmp/import_runs/shanghai_2023_spring_solution_qwen36/source_runs/shanghai_2023_spring_solution_qwen36_parallel`，14 页 mixed 输入，题面、参考答案与解析交织在同一文件中。随后用 `--image-kind both` 触发一次全图片响应，输出目录为 `tmp/probe_image_parallel_step3_render_shanghai_2023_spring_qwen36/shanghai_2023_spring_solution_qwen36_parallel/image_parallel_step3`：一次响应返回 21 个合法 tool calls，坏 tool 名称数为 0，题号 1-21 连续，`answer_latex` 填充 21 题，`analysis_latex` 填充 21 题，初始 `qb_success_count=21`、`qb_error_count=0`。Step3.5 首轮审计 `flagged_question_count=11`、`before_finding_count=30`，候选修复后 `after_finding_count=1`，但 q16、q21 因 `content_fingerprint_changed` 等原因未写回。对最终 question bank 做 `--dry-run` 本地复核后，实际剩余 q16、q21 两题共 9 个 LaTeX findings。Step4 资产 `asset_count=4`、`assigned_count=4`、`risk_count=4`，分配到题 14、15、17、18；Step5 渲染入口：`tmp/probe_image_parallel_step3_render_shanghai_2023_spring_qwen36/shanghai_2023_spring_solution_qwen36_parallel/image_parallel_step3/rendered_question_bank_mathjax/shanghai_2023_spring_solution_qwen36_parallel/index.html`。
- 按用户要求将同一 2023 春考解析卷 probe 重跑为 Step3.5 full 模式：复用已成功的 21 个 `parsed_tool_calls`，不重新调用图像 Step3；full Step3.5 `question_count=21`、`before_finding_count=30`、`normalized_count=21`、`error_count=0`、`applied_question_numbers` 数量为 19、无 rejected。随后重新接 Step4 和 Step5，Step4 `asset_count=4`、`assigned_count=4`、`changed_question_count=21`；最终渲染入口仍为 `tmp/probe_image_parallel_step3_render_shanghai_2023_spring_qwen36/shanghai_2023_spring_solution_qwen36_parallel/image_parallel_step3/rendered_question_bank_mathjax/shanghai_2023_spring_solution_qwen36_parallel/index.html`。对 full 后最终 question bank 做 dry-run 审计，剩余 q21 的 1 个 `left_right_count_mismatch` finding，q16 已清空；当前图形资产落在题 14、15、17，其中题 17 挂载 2 个图。
- 按用户要求测试“只给 14 页资产标注原图，文本只给资产标签列表，不给 OCR，仍走 tool schema”：命令使用 `qwen3.6-plus`、`--image-kind asset_marked`、`--prompt-compact-mode asset_labels_only`、`--hide-block-text`、`parallel_tool_calls=true`、`enable_thinking=false`。输出目录为 `tmp/probe_image_parallel_step3_render_shanghai_2023_spring_qwen36_assetlabels_noocr/shanghai_2023_spring_solution_qwen36_parallel/image_parallel_step3`。
- 该测试的实际 `input_compact.json` 中，14 个 `paper_pages` 只含 `page` 和 `asset_labels` 两个键；资产标签总数 4，分别为 `M-V05-P01`、`M-V06-P01`、`M-V08-P01`、`M-V09-P01`；无 OCR 文本、普通文本块标签和 bbox。
- 该测试 Step3 一次响应返回 21 个 `submit_image_question_record` tool calls，坏工具名 0，解析出 21 道题，题号 1-21 连续，`qb_success_count=21`、`qb_error_count=0`，Step3 图像调用耗时 270.848 秒；模型对 21 题均填充 `answer_latex` 和 `analysis_latex`，但所有 `source_question_labels` 与 `source_answer_labels` 均为空，因为 prompt compact 没有提供普通文本标签。
- 该测试 Step3.5 full 首轮 `question_count=21`、`normalized_count=20`、`error_count=1`，第 11 题出现一次百炼 SSL EOF 网络错误；随后只对第 11 题用同一 `qwen3.6-plus` 和 `tool_calling` 单题重试，`normalized_count=1`、`error_count=0`，并重新生成 Step5 HTML。
- 该测试 Step4 `asset_count=4`、`assigned_count=4`、`noise_count=0`、`uncertain_count=0`、`risk_count=4`，最终资产落在题 14、15、17，其中题 17 挂载 `M-V08-P01`、`M-V09-P01`；Step5 渲染入口为 `tmp/probe_image_parallel_step3_render_shanghai_2023_spring_qwen36_assetlabels_noocr/shanghai_2023_spring_solution_qwen36_parallel/image_parallel_step3/rendered_question_bank_mathjax/shanghai_2023_spring_solution_qwen36_parallel/index.html`。
- 该测试最终 dry-run 审计 `question_count=21`、`flagged_question_count=1`、`before_finding_count=1`，剩余问题为第 19 题 `analysis_latex` 中一段模型自述触发 `math_relation_outside_math`；内容含“原文计算可能有误”“我们遵循原文结论”等说明，说明在不给 OCR 的资产标签模式下，解析卷图片转写存在模型自述/解释性幻觉风险。
- 同一输入形态改用 `qwen3.5-flash` 测试，输出目录为 `tmp/probe_image_parallel_step3_render_shanghai_2023_spring_qwen35_flash_assetlabels_noocr/shanghai_2023_spring_solution_qwen36_parallel/image_parallel_step3`。Step3 一次响应返回 21 个 `submit_image_question_record` tool calls，坏工具名 0，题号 1-21 连续，`qb_success_count=21`、`qb_error_count=0`，Step3 图像调用耗时 85.149 秒；usage 为 `prompt_tokens=29664`、`image_tokens=27482`、`text_tokens=2182`、`completion_tokens=14093`、`total_tokens=43757`。
- `qwen3.5-flash` 测试中，21 题均填充 `answer_latex` 和 `analysis_latex`，未命中“原文可能”“检查原文”“我们遵循”等模型自述关键词。Step3.5 full 首轮 `question_count=21`、`normalized_count=19`、`error_count=2`、`before_finding_count=22`，第 5、14 题均为百炼 SSL EOF 网络错误；随后分别单题重试成功，并重新生成 Step5 HTML。
- `qwen3.5-flash` 测试最终 dry-run 审计 `question_count=21`、`flagged_question_count=0`、`before_finding_count=0`。Step4 `asset_count=4`、`assigned_count=4`，最终资产落在题 14、15、17、18；渲染入口为 `tmp/probe_image_parallel_step3_render_shanghai_2023_spring_qwen35_flash_assetlabels_noocr/shanghai_2023_spring_solution_qwen36_parallel/image_parallel_step3/rendered_question_bank_mathjax/shanghai_2023_spring_solution_qwen36_parallel/index.html`，本地 HTTP 入口验证为 `http://127.0.0.1:8767/shanghai_2023_spring_solution_qwen36_parallel/index.html`。

### 新增 Step2 并行工具调用探测脚本

修改范围：

- 新增 `assets/v11_runtime/probe_step2_parallel_tool_calls.py`

修改原因：

- 需要验证百炼 `parallel_tool_calls` 在“每道顶层题调用一次工具”的 Step2 范围识别实验中是否可用。
- 当前正式 Step2 仍是单次 `submit_step2_ranges` 返回完整 `question_ranges` 数组，不能消费多个 tool call；该脚本只做独立探测，不改正式 pipeline。

影响：

- 新脚本从已有 `source_runs/<run_id>` 构造 Step2 compact payload，默认 OCR-only 调用百炼。
- 脚本要求模型对每个顶层题号各调用一次 `submit_step2_question_range`，保存 `raw_response.json`、`parsed_tool_calls.json`、`normalized_ranges.json` 和 `summary.json`。
- 不修改 Step2 正式 CLI、prompt、schema 或产物布局。

验证：

- 已通过 `python.exe -m py_compile assets/v11_runtime/probe_step2_parallel_tool_calls.py`。
- 已通过 `python.exe assets/v11_runtime/probe_step2_parallel_tool_calls.py --help`。
- 已用百炼 `qwen-plus` 对 `tmp/import_runs/qingpu_2022_sep_page1/source_runs/qingpu_2022_sep_quality_page1` 做 OCR-only 单页探测：一次响应返回 12 个 `submit_step2_question_range` tool calls，归一化后 12 道题，`invalid_range_count=0`，缺号列表为空。
- 探测输出目录：`tmp/probe_step2_parallel_tool_calls/qingpu_2022_sep_quality_page1/paper_pure_paper`。

### 同步 Step3 外置 prompt 与运行实现

修改范围：

- 修改 `assets/v11_runtime/prompts/zh_CN/step3_question_json.md`

修改原因：

- Step3 外置 prompt 是后续 prompt loader 的目标来源，但此前落后于 `step3_question2json.py` 中真实发送给模型的 prompt。
- 若直接迁移到外置 prompt，会丢失标准 OCR 模式的字段说明、image-only 解析保真规则、issue type 区分和 tool calling 追加约束。

影响：

- 仅同步外置 prompt 文档，不修改 Python 运行逻辑、CLI、schema 或产物布局。
- 外置文档现在记录标准 OCR 模式、image-only 模式和 tool calling 请求中的关键运行约束。

验证：

- 已对照 `step3_question2json.py` 中 `USER_PROMPT`、`IMAGE_ONLY_USER_PROMPT_TEMPLATE` 和 `call_tool_calling()` 补齐缺失规则。
- 未运行 pipeline；本次无运行代码变更。

## 2026-06-03

### 修复 Step2 同题号分部误拆

修改范围：

- 修改 `assets/v11_runtime/step2_layout.py`
- 修改 `assets/v11_runtime/prompts/zh_CN/step2_layout.md`

修改原因：

- 单页导入含 `21-I`、`21-II` 分部的第 21 题时，Step2 会把同一顶层题号拆成两个 question range，导致 Step3 对同一 `question_no` 生成两次记录，后一次覆盖前一次。

影响：

- Step2 prompt 明确规定 `21-I`、`21-II`、`21-Ⅰ`、`21-Ⅱ` 属于同一顶层题号。
- Step2 `normalize_ranges()` 会合并重复 `question_no` 的范围，保留最早 start、最晚 end、合并视觉标签，并记录 `merged_duplicate_question_range_count`。

验证：

- 已对 `photo_math_page5_q21_only_20260603` 重跑 pipeline。Step2 只输出 1 个范围，最终 question bank 只含 1 条 `question_no=21` 记录，内容覆盖 21-I 和 21-II，并保留图 4 资产。

## 2026-06-02

### 新增 v11 prompts 外置规范

修改范围：

- 新增 `assets/v11_runtime/prompts/zh_CN/README.md`
- 新增 `assets/v11_runtime/prompts/zh_CN/step2_layout.md`
- 新增 `assets/v11_runtime/prompts/zh_CN/step3_question_json.md`
- 新增 `assets/v11_runtime/prompts/zh_CN/step35_latex_audit.md`
- 新增 `assets/v11_runtime/prompts/zh_CN/step4_assets.md`
- 新增 `assets/v11_runtime/prompts/zh_CN/step5_render.md`

修改原因：

- 固定 Step2-Step5 的 prompt、给大模型的输入 payload 和返回格式契约。
- 为后续把内嵌 prompt 迁移到 `exam_import_v11.prompts.zh_CN` 做准备。

影响：

- 仅新增文档和 prompt 规范副本。
- 当前 Python 运行代码尚未改为从 `prompts/zh_CN` 加载 prompt。
- 不改变任何 CLI、产物布局或 pipeline 行为。

验证：

- 已确认 `step5_vlm_html_mathjax_render.py` 不调用大模型，因此 `step5_render.md` 只记录输入输出契约。
