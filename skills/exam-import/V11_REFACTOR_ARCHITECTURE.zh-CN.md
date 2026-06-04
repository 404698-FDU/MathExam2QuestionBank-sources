# v11 Runtime 重构架构与职责说明

本文档描述 `exam-import` skill 内置 v11 runtime 的目标架构、每一部分职责、保留的主线功能契约，以及迁移时不得破坏的边界。

## 1. 重构目标

v11 runtime 的重构目标不是重写流程，而是把现有脚本堆整理成边界清晰、可测试、可替换的包结构，同时保留现有导入主线。

必须保留的主线是：

```text
import_spec.json
  -> scripts/validate_import_spec.py
  -> scripts/v11_source_import.py
  -> assets/v11_runtime/run_pipeline.py
  -> Step2/Step3/Step3.5/Step4/Step5
  -> 题库 JSON、审计结果、资产同步、MathJax 渲染入口、运行证据
```

重构后，旧脚本名仍应可运行；旧脚本只作为兼容入口，把实际逻辑转交给新的 `exam_import_v11` 包。

## 2. 顶层职责

### `SKILL.md`

职责：

- 定义 Codex 何时应该使用 `exam-import` skill。
- 规定导入流程必须先生成 import spec，再验证，再运行，再报告证据。
- 规定失败策略：缺文件、页码范围不清、OCR 不完整、模型配置不完整时应尽早失败。
- 只保留操作规则和 workflow，不承载具体 Python 实现细节。

不职责：

- 不保存运行产物。
- 不描述每个内部函数的实现。
- 不作为实验记录或临时修复记录。

### `FUNCTIONAL_SPEC.zh-CN.md`

职责：

- 定义 skill 当前功能规格。
- 描述输入模式、产物布局、Step2-Step5 的行为契约。
- 记录可见的功能要求，例如 tool calling、Step3 schema、MathJax 渲染、证据报告。

不职责：

- 不记录每次重构提交的流水账。
- 不保存临时实验方案。

### `V11_REFACTOR_ARCHITECTURE.zh-CN.md`

职责：

- 描述重构后的代码架构。
- 定义每个包、模块、入口文件的职责边界。
- 作为后续拆分代码时的对照表。

不职责：

- 不替代 `FUNCTIONAL_SPEC.zh-CN.md` 的功能规格。
- 不作为运行报告。

### `REFACTOR_LOG.zh-CN.md`

职责：

- 每次修改 v11 runtime 代码后记录变更。
- 每条记录至少包含日期、修改范围、为什么改、影响的 CLI 或产物、验证结果。

不职责：

- 不写未执行的设想。
- 不记录大段终端输出。

## 3. 兼容入口职责

### `scripts/validate_import_spec.py`

职责：

- 作为旧命令兼容入口。
- 读取 import spec。
- 校验输入模式、源文件、页码范围、模型配置、缓存策略。
- 发现默认 v11 runtime。
- 生成两类命令：source import 命令和 pipeline 命令。
- 输出 JSON 或 PowerShell 命令。

重构后职责：

- 仅解析 CLI 参数。
- 调用 `exam_import_v11.cli.validate_spec.main()`。
- 不再直接承载校验细节。

不职责：

- 不运行 MinerU。
- 不运行 Step2-Step5。
- 不手工修正 spec。

### `scripts/v11_source_import.py`

职责：

- 作为旧命令兼容入口。
- 接收本地 PDF、页码范围、输入模式、MinerU 配置。
- 创建 `runs/source_runs/<run_id>`。
- 写入 `source_item.json`。
- 为 paper/answer/mixed 部分生成 `ocr_blocks.json`。

重构后职责：

- 仅解析 CLI 参数。
- 调用 `exam_import_v11.cli.source_import.main()`。
- 不再直接依赖历史批处理脚本里的通用函数。

不职责：

- 不运行 Step2。
- 不生成题库 JSON。
- 不静默复用旧 OCR。
- 不创建空白页图占位。

### `assets/v11_runtime/run_pipeline.py`

职责：

- 作为旧命令兼容入口。
- 串联 Step2、Step3、Step3.5、Step4、Step5。
- 维护旧 CLI 参数兼容。

重构后职责：

- 仅解析 CLI 参数。
- 调用 `exam_import_v11.cli.run_pipeline.main()`。
- 负责兼容旧参数名，不负责具体业务逻辑。

不职责：

- 不直接包含 Step2/Step3/Step4 的实现细节。
- 不把运行证据散写到不可预测位置。

### `assets/v11_runtime/run_answer_patch.py`

职责：

- 为已有 `pure_paper` run 补答案。
- 保留已有题面 OCR 和 Step2 题面切分结果。
- 只抽取答案侧 OCR。
- 重建答案范围。
- 调用 pipeline 的 `--skip-step2` 后续流程。

重构后职责：

- 仅解析 CLI 参数。
- 调用 `exam_import_v11.cli.answer_patch.main()`。

不职责：

- 不重建已有题面 OCR。
- 不改变原纯题面切分边界。

## 4. 新包结构职责

建议新增包：

```text
assets/v11_runtime/exam_import_v11/
  cli/
  core/
  schemas/
  sources/
  llm/
  steps/
  prompts/zh_CN/
  legacy/
  tests/
```

## 5. `exam_import_v11.cli` 职责

`cli` 层只负责命令行适配，不承载业务规则。

### `cli/validate_spec.py`

职责：

- 解析 `--spec`、`--v11-root`、`--runs-root`、`--write-normalized`、`--emit`。
- 调用 schema 层完成 spec 解析与校验。
- 调用 command builder 生成可执行命令。
- 输出 JSON 或 PowerShell。

不职责：

- 不检查 OCR 内容质量。
- 不运行 pipeline。

### `cli/source_import.py`

职责：

- 解析 source import CLI 参数。
- 把 CLI 参数转换为 `SourceImportRequest`。
- 调用 `sources` 层执行 PDF 拆页和 OCR 抽取。
- 写出 source import summary。

不职责：

- 不理解 Step2/Step3 schema。
- 不做答案补丁逻辑。

### `cli/run_pipeline.py`

职责：

- 解析 pipeline CLI 参数。
- 构建 `PipelineRequest`。
- 调用 `steps` 层按顺序执行。
- 汇总 batch summary 和证据摘要。

不职责：

- 不直接拼接 prompt。
- 不直接调用 LLM provider。

### `cli/answer_patch.py`

职责：

- 解析 answer patch CLI 参数。
- 校验已有 pure-paper run 的必要产物存在。
- 调用 `sources` 层抽取答案 OCR。
- 调用 `steps.step2_layout` 的答案范围重建能力。
- 调用 pipeline 后续步骤。

不职责：

- 不改变已有题面 OCR。
- 不覆盖已有题面 Step2 范围。

## 6. `exam_import_v11.core` 职责

`core` 层放跨步骤的稳定基础设施。

### `core/paths.py`

职责：

- 定义 runtime root、skill root、runs root 的解析规则。
- 生成标准产物路径。
- 统一 source runs、step2、question bank、audit、render 的目录计算。
- 提供路径安全检查，例如强制清理目录必须落在允许 root 内。

不职责：

- 不读取 OCR。
- 不写业务 JSON。

### `core/io.py`

职责：

- 提供 `read_json`、`write_json`、`write_jsonl`、`write_text`。
- 统一 UTF-8、`ensure_ascii=False`、缩进格式。
- 保证写文件前创建父目录。

不职责：

- 不做 schema 校验。
- 不做业务默认值填充。

### `core/evidence.py`

职责：

- 收集完整流程运行后的证据。
- 输出成功数、错误数、Step2 题量、答案缺失、Step3 summary、Step3.5 审计、Step4 资产数量、同步数量、Step5 渲染入口。
- 生成稳定的 evidence report。

不职责：

- 不掩盖失败。
- 不把失败项改成成功。

### `core/artifact_contracts.py`

职责：

- 定义每一步必须产生哪些文件。
- 提供 `require_artifact()`、`assert_source_run_complete()`、`assert_step2_complete()` 等检查。
- 在 pipeline 阶段之间做明确的产物存在性检查。

不职责：

- 不生成缺失产物。
- 不执行 fallback。

## 7. `exam_import_v11.schemas` 职责

`schemas` 层定义结构化数据，不做 I/O 和网络调用。

### `schemas/import_spec.py`

职责：

- 定义 `ImportSpec`。
- 校验 `run_id`、`input_mode`、source 文件、页码范围、模型配置、cache policy。
- 处理 mode alias，例如 `answer_patch` -> `answer_patch_for_existing_pure_paper`。
- 输出 normalized spec。

不职责：

- 不生成命令。
- 不运行文件抽取。

### `schemas/source_item.py`

职责：

- 定义 `source_item.json` 的字段。
- 记录 run id、mode、source shape、paper/answer 路径、页码范围、source rule、创建时间。

不职责：

- 不保存 OCR blocks。
- 不保存 Step2 结果。

### `schemas/step2.py`

职责：

- 定义 Step2 范围、label index、alignment、pipeline summary 的结构。
- 校验题号、label、范围字段的基本一致性。

不职责：

- 不决定如何切题。
- 不调用 LLM。

### `schemas/step3.py`

职责：

- 定义 `Step3Record` 和 strict schema。
- 固定字段：`schema_version`、`question_no`、`question_type`、`stem_latex`、`options_latex`、`answer_latex`、`analysis_latex`、`rubric_latex`、`issues`。
- 禁止额外字段。
- 校验数组项单行非空、题型枚举、issue severity。

不职责：

- 不做 prompt。
- 不自动补答案或解析。

### `schemas/audit.py`

职责：

- 定义 Step3.5 审计 finding、修复结果、audit summary。
- 约束审计前后记录和修复状态。

不职责：

- 不直接改 question bank。
- 不决定是否调用 LLM。

## 8. `exam_import_v11.sources` 职责

`sources` 层只处理源文档到 OCR/source run。

### `sources/page_ranges.py`

职责：

- 解析页码范围语法。
- 把 `1-3,5` 转成页码列表。
- 生成稳定 slug。
- 校验页码范围格式。

不职责：

- 不读取 PDF。

### `sources/pdf_split.py`

职责：

- 按页码范围拆 PDF。
- 生成 `_split_sources/<part>_pages_<range>.pdf`。
- 处理强制覆盖。

不职责：

- 不调用 MinerU。
- 不做 OCR。

### `sources/mineru_client.py`

职责：

- 处理 MinerU token。
- 提交文件。
- 轮询结果。
- 下载并解压结果包。
- 记录 MinerU 原始响应和必要的 redacted 信息。

不职责：

- 不解析 Step2。
- 不把非 PDF 结果伪造成真实页图。

### `sources/ocr_extract.py`

职责：

- 把 MinerU 结果转成 `ocr_blocks.json`。
- 生成真实 `pages/page_*.png`。
- 对非 PDF 源优先使用 MinerU 返回的 `*_origin.pdf`、`origin.pdf`、`*layout*.pdf` 或 `layout.pdf` 渲染页图。
- 如果没有可渲染 PDF 或真实页图，立即失败。

不职责：

- 不创建空白页图。
- 不静默跳过图片缺失。

### `sources/source_run.py`

职责：

- 根据 input mode 生成 part plan。
- 创建 `runs/source_runs/<run_id>`。
- 写入 `source_item.json`。
- 调用 `pdf_split` 和 `ocr_extract` 生成 paper/answer OCR。
- 输出 source import summary。

不职责：

- 不运行 Step2-Step5。

### `sources/wechat_import.py`

职责：

- 处理微信文章源的 HTML、图片、PDF 派生文件。
- 生成能进入 source run 的本地源文件或 OCR 输入。

不职责：

- 不混入通用 PDF import 主线。
- 不把微信特例写进普通 PDF import。

## 9. `exam_import_v11.llm` 职责

`llm` 层负责模型调用、结构化输出、限流和并发。

### `llm/providers.py`

职责：

- 定义 provider：`siliconflow`、`bailian`、`bailian_batch`、`env`。
- 保存厂商级接入规则：chat endpoint、base URL、token 环境变量、token 文件、批量 endpoint、OpenAI-compatible 请求差异。
- 保存厂商级能力开关：是否支持 `json_schema`、是否支持 tool calling、是否支持 `enable_thinking`、thinking 参数应使用 `enable_thinking` 还是 `thinking: {"type": ...}`。
- 提供 `resolve_provider(name)` 和 `configure_provider_env(provider, env)` 这类薄入口。

不职责：

- 不写业务 prompt。
- 不保存具体模型的 TPM、token estimator、默认 worker 数或上下文长度。
- 不保存某一次 Step 调用应使用的结构化输出方式、工具名、schema、temperature 或 timeout。

### `llm/model_configs.py`

职责：

- 保存模型级静态配置，例如 provider、模型名、默认 token estimator、上下文窗口、建议 TPM bucket、是否默认关闭 thinking、已验证的结构化输出能力。
- 允许同一 provider 下不同模型有不同能力，例如某个模型支持 tool calling 但不稳定支持 `json_schema`。
- 提供 `resolve_model_config(model_name)`，只返回模型配置，不构造请求体。

不职责：

- 不设置 endpoint 或读取 API token。
- 不决定本次调用使用 Step3 标准模式、image-only 模式还是 Step4 资产归属模式。
- 不根据失败结果切换模型或加入 fallback。

### `schemas/call_spec.py` / `llm/call_spec_loader.py`

职责：

- 定义并校验调用时显式输入的 `call_spec.json`。
- 每一次 LLM 调用都应读取一个明确的 JSON call spec，而不是从代码里选择隐式 profile。
- `call_spec.json` 描述 Step 名、prompt 名、model、provider、structured output 模式、tool 名、tool schema 引用、temperature、top_p、timeout、thinking 开关、是否传图片、是否传 OCR。
- 把 `call_spec.json`、model config、provider capability 合并成最终请求前的明确 `CallSpec`。
- 在 provider 不支持所选 structured output 或 thinking 参数时尽早失败。

不职责：

- 不保存内置调用 profile。
- 不保存 API key。
- 不保存厂商 endpoint。
- 不执行 HTTP 请求。
- 不把模型输出改写成业务成功结果。

### `llm/client.py`

职责：

- 发送 chat completion 请求。
- 处理 HTTP 错误、限流错误、临时错误。
- 返回原始 payload。
- 按配置执行明确重试。

不职责：

- 不解析 Step3 schema。
- 不把模型失败改成空结果。

### `llm/structured_output.py`

职责：

- 构建 `json_schema`、`json_object`、`tool_calling` 请求格式。
- 强制 tool choice。
- 从 tool call 中解析 arguments。
- 如果模型未返回 tool call，应立即失败。

不职责：

- 不修正模型输出里的业务内容。

### `llm/token_budget.py`

职责：

- 统一 TPM 限制。
- 管理 token budget pool。
- 估算文本和图片 token。
- 记录 token budget 事件。

不职责：

- 不决定题目分配策略。

### `llm/concurrency.py`

职责：

- 控制 worker 数。
- 支持多模型任务分配。
- 对失败项保留明确错误结果。

不职责：

- 不因为限流加入静默 fallback。

## 10. `exam_import_v11.steps` 职责

`steps` 层是主线业务，每一步都只读固定输入、写固定输出、返回 summary。

### `steps/step2_layout.py`

职责：

- 读取 `source_runs/<run_id>`。
- 根据 mode 切题面范围和答案范围。
- 建立 OCR block label 映射。
- 生成 question packets、answer packets、range files、alignment。
- 生成 Step2 裁剪图。
- 写 `pipeline_summary.json`。

不职责：

- 不生成题库 JSON。
- 不归属图片到具体字段。

### `steps/step3_question_json.py`

职责：

- 读取 Step2 范围和裁剪图。
- 构造 per-question payload。
- 调用 LLM 生成单题 `Step3Record`。
- 校验 schema。
- 写 per-question 原始响应、输入、校验结果、最终题库记录。
- 汇总 `question_bank.json`、`question_bank.jsonl`、`summary.json`。

不职责：

- 不手工补全模型漏掉的答案解析。
- 不处理 Step4 资产归属。

### `steps/step35_latex_audit.py`

职责：

- 审计 Step3 输出中的 LaTeX、HTML entity、choice blank、数学分隔符、图片标号等问题。
- 必要时调用 LLM 修复格式问题。
- 保持 question_no、question_type、字段结构、答案内容、解析内容不被随意改写。
- 输出 audit before/after、fixed question bank、summary。

不职责：

- 不凭空补解题过程。
- 不把失败修复隐藏成成功。

### `steps/step4_assets.py`

职责：

- 读取 Step2 视觉资产和 Step3 题库记录。
- 判断图片、表格、图形属于题干、选项、答案、解析或风险项。
- 同步资产元数据到 question bank。
- 输出资产分配 summary 和 sync summary。

不职责：

- 不改写题干正文。
- 不替代 Step3 的内容转写。

### `steps/step5_render.py`

职责：

- 读取 question bank 和资产。
- 生成 MathJax HTML 审核页。
- 同步渲染所需图片。
- 输出稳定渲染入口路径。

不职责：

- 不修改 question bank JSON。
- 不修复 LaTeX 内容。

## 11. `exam_import_v11.prompts.zh_CN` 职责

职责：

- 保存面向中文数学试题的 prompt。
- 每个 Step 的 prompt 独立文件管理。
- prompt 全部使用中文。
- prompt 只描述任务、字段规则、边界和禁止行为。
- 保存给大模型的输入 payload 结构、图片输入位置、结构化输出方式和工具返回格式。
- Step5 不调用大模型，但仍应在 `prompts/zh_CN/step5_render.md` 保存渲染输入输出契约，避免 Step2-Step5 接口记录断层。

不职责：

- 不中英混杂。
- 不把 prompt 散落在多个业务函数内部。
- 不在 prompt 中加入与当前 Step 无关的历史实验说明。

## 12. `exam_import_v11.legacy` 职责

职责：

- 暂存仍需兼容但不属于主线的历史脚本。
- 包括上海 2017-2026 批处理、老版 question bank app、旧实验 pipeline、百炼 batch 实验脚本。
- 为迁移期提供引用，避免直接删除导致历史命令失效。

不职责：

- 不作为新主线依赖。
- 不继续堆放新的通用逻辑。
- 不保存运行产物。

## 13. `exam_import_v11.tests` 职责

职责：

- 验证 import spec 校验。
- 验证命令兼容。
- 验证 source run 布局。
- 验证 Step3 schema。
- 验证 artifact contracts。
- 使用小 fixture，不依赖大规模历史 runs。

不职责：

- 不调用真实 MinerU 或真实 LLM，除非显式标记为集成测试。
- 不读取历史 tmp 产物作为默认测试输入。

## 14. 运行产物职责边界

运行产物只应出现在 `runs_root` 下，不应混入源码目录。

推荐布局：

```text
runs/
  source_runs/<run_id>/
  step2_exam_blocks/<run_id>_raw_units/
  question_bank/<run_id>/
  reviews_step3_5_latex_audit/<run_id>/
  rendered_question_bank_mathjax/<run_id>/
  reports/
```

`assets/v11_runtime/` 只保留源码、prompt、schema、小型测试 fixture 和 requirements。

不得保留：

- `__pycache__/`
- `tmp/`
- 历史大图、大 PDF、大 HTML 渲染产物
- 旧实验 run 结果
- 只为一次实验存在的缓存

## 15. 迁移验收标准

每完成一个模块迁移，至少验证：

- 旧 CLI 还能运行到同一入口。
- `validate_import_spec.py` 输出的命令不破坏旧参数。
- source import 仍生成 `source_item.json` 和 `ocr_blocks.json`。
- pipeline 仍能按 Step2-Step5 顺序运行。
- 完整流程后能报告真实证据。
- 没有新增静默 fallback。
- 没有手工改最终 question bank JSON 来掩盖失败。

## 16. 变更记录

- 2026-06-02：新增本文档，定义 v11 runtime 重构后的包结构和每一部分职责。此次修改只新增架构文档，不修改运行代码。
- 2026-06-02：新增 `assets/v11_runtime/prompts/zh_CN/` prompt 规范目录，保存 Step2-Step5 的 prompt、模型输入 payload 和返回格式契约；当前不改变运行代码。
