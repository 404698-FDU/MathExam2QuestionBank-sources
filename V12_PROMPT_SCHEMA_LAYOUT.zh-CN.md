# v12 Prompt 与 JSON Schema 结构约束

目标：把 v12 的 prompt、tool schema、运行时 dataclass/schema、步骤算法文档分层放置，避免“prompt 改了、schema 没改”或“schema 改了、prompt 还在旧字段”的结构性漂移。

## 1. 分层原则

v12 中至少分八层：

1. `prompts/zh_CN/*.prompt.md`
   保存运行时实际读取的中文 prompt 文本，固定采用 `meta/system/user` 三段结构。

2. `prompts/zh_CN/*.md`
   保存仍需随运行时 prompt 一起审阅的步骤算法和上下游流转说明。

3. `doc/zh_CN/*.md`
   保存旧长版 prompt 契约文档，作为历史设计证据保留，不再作为运行时读取源，也不作为 `check_contracts.py` 的机器校验目标。

4. `schemas/*.py`
   保存运行时代码真正使用的数据结构校验，例如：
   - `qa_alignment_v2`
   - `QuestionRecord`
   - `QuestionRangesResult`
   - `VisualAssetReview`
   - `AnswerTableReview`
   - `CallSpec`

5. `provider_config/**/*.json`
   保存 provider 和 model 的静态外置配置。

6. `tool_schemas/*.schema.json`
   保存真正发给 tool calling API 的 JSON Schema。

7. `call_specs/*.json`
   保存每一步实际调用使用的 `call_spec_v1` JSON。

8. `steps/*.py`
   消费前面各层的稳定产物，不再自己发明字段。

## 2. 各层职责

### 运行时 prompt 层

职责：

- 定义模型实际看到的中文 system/user 指令。
- 提供稳定、统一、轻量的 `meta/system/user` 结构。
- 允许运行时代码只按固定 section 读取，不再按 heading path 解析。

不职责：

- 不内嵌整份 JSON Schema。
- 不承担算法说明、payload 示例和步骤长文档。
- 不保存业务代码里的 merge 逻辑。

### 算法与流转文档层

职责：

- 保存当前仍参与设计审阅的步骤算法、输入输出流转和失败边界。
- 给人工审阅和实现对齐提供上下文。

不职责：

- 不要求运行时逐段解析其中的多级标题。

### 历史证据文档层

职责：

- 保存旧长版 prompt 契约文档，保留历史设计和审计证据。
- 允许运行时 prompt 在 `meta.source_doc` 中指向历史来源，方便追溯。

不职责：

- 不作为运行时 prompt source of truth。
- 不作为当前 `check_contracts.py` 的 heading path 校验目标。

### 运行时 schema 层

职责：

- 对本地读写 JSON 做强校验。
- 保证跨步骤产物有稳定结构。
- 提供 dataclass / validator 给 Step2-Step5 本地逻辑使用。

不职责：

- 不直接承担模型 prompt 文本。
- 不关心具体哪家 provider。

### tool schema 层

职责：

- 生成发给 LLM tool calling 的 JSON Schema。
- 和 prompt 中的工具字段保持一一对应。
- 明确约束 `additionalProperties`、`required`、枚举和值类型。
- 独立落盘，避免 schema 继续藏在 Python 代码里的大字典中。

不职责：

- 不承担本地 merge/sync/render 逻辑。
- 不替代运行时 dataclass 校验。

### provider/model 配置层

职责：

- 保存 provider 和 model 的静态能力与接入规则。
- 让 `call_spec.json` 只表达“这次怎么调”，不重复保存 endpoint、token 来源、上下文窗口等稳定信息。

不职责：

- 不保存步骤 prompt。
- 不保存单次调用温度、超时、重试等 step 级参数。

### 步骤实现层

职责：

- 调 prompt loader 读取 prompt。
- 调 tool schema 注册表拿 JSON Schema。
- 调运行时 schema 校验模型输出和本地产物。
- 实现同步、裁剪、写回和渲染。

不职责：

- 不在步骤代码里重新定义字段含义。
- 不硬编码一套与 prompt/schema 平行的新结构。

## 3. 推荐文件布局

```text
assets/v12_runtime/
  doc/
    zh_CN/
      step2_layout.md
      step3_question_json.md
      step35_latex_audit.md
      step4_assets.md
  provider_config/
    providers/
      *.json
    models/
      *.json
  prompts/
    zh_CN/
      step2_layout.prompt.md
      step2_qa_alignment_contract.md
      step2_crop_algorithm.md
      step3_question_json.prompt.md
      step35_latex_audit.prompt.md
      step4_visual_assets.prompt.md
      step4_answer_tables.prompt.md
      step4_asset_placeholder_algorithm.md
      step5_render.md
      step_pipeline_processing_flow.md
  exam_import/
    schemas/
      call_spec.py
      import_spec.py
      question_ranges.py
      qa_alignment.py
      question_record.py
      visual_asset.py
      answer_table.py
      pipeline_summary.py
    llm/
      tool_schemas.py
  call_specs/
    *.json
  tool_schemas/
    step2_question_ranges.schema.json
    question_record.schema.json
    visual_asset_review.schema.json
    answer_table_review.schema.json
```

## 4. 稳定引用规则

为保证结构稳健，每个步骤应固定三种引用：

- `prompt_ref`
  例如 `step3_question_json`，映射到 `step3_question_json.prompt.md`

- `tool_name`
  例如 `image_only_question_standardization`

- `tool_schema_ref`
  例如 `QuestionRecord`

- `call_spec_path`
  例如 `assets/v12_runtime/call_specs/step3_question_json.bailian.qwen-vl-max.tool_calling.json`

运行时步骤通过 `prompt_ref`、`tool_name`、`tool_schema_ref` 和 `call_spec_path` 组合取资源，不直接在步骤代码里复制大段 prompt 或手写另一套字段解释。

## 5. 当前 v12 的现实状态

当前 v12 已做到：

- 运行时 prompt 已独立放在 `prompts/zh_CN/*.prompt.md`
- 当前算法和流转说明仍独立放在 `prompts/zh_CN/*.md`
- 旧长版 prompt 契约文档已迁移到 `doc/zh_CN/*.md`，只作为历史证据
- 运行时 schema 已独立放在 `exam_import/schemas/`
- provider/model 静态配置已独立放在 `provider_config/**/*.json`
- tool schema 已独立放在 `tool_schemas/*.schema.json`
- `exam_import/llm/tool_schemas.py` 只保留轻量 alias 和加载逻辑
- `exam_import/cli/check_contracts.py` 已可机器校验：
  - `prompt_ref -> prompt 文件`
  - 运行时 prompt 的 `meta/system/user` section
  - `tool_schema_ref -> *.schema.json`
  - 核心 tool schema 的顶层 required 字段
  - `call_spec_path -> call_spec_v1 JSON -> provider/model/prompt/tool`

当前还没有完全做到的点：

- tool schema 还不是从 prompt 文档自动抽取，而是从独立 schema 文件读取。
- 运行时 schema 和 tool schema 之间的校验还主要停留在关键字段层面，不是自动从 dataclass 反推 JSON Schema。

这属于当前阶段的过渡实现，不是最终理想结构。

## 6. 下一步最稳的收敛方向

为了进一步提高健壮性，后续建议继续收敛到：

1. 每个运行时 prompt 继续保持一文件一调用单元，不再回退到“一个文档里靠 heading path 区分多个 prompt”。
2. 每个步骤新增一个独立 schema 文档或 schema 目录项，只存 JSON Schema，不混在运行时 prompt 中。
3. `tool_schemas.py` 只做 alias 和读取，不再承载 schema 主体。
4. 继续增强 `check_contracts.py`：
   - prompt 引用的 `tool_name` 必须存在
   - prompt 引用的 `tool_schema_ref` 必须存在
   - schema 与运行时 dataclass 的关键字段必须一致

## 7. 当前建议

如果继续推进 v12，最合适的下一步不是再把更多 schema 塞进 prompt 文档，而是：

- 新增一个专门的 schema 目录或 schema 文档索引
- 让 prompt 文档只保留中文说明和字段语义
- 让 JSON Schema 有独立的稳定落点

这样 prompt、tool schema、运行时 schema 三层才不会继续耦在一起。
