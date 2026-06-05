# 导入运行记录

## 2026-06-05 shanghai_2017_autumn_paper_answer

- 任务：导入 `gaokaomath_shanghai/hi_quality/2017/[EduVault] 2017年6月 数学秋考.pdf` 与对应参考答案 PDF。
- 运行配置：v12 `paper_plus_answer`，`max_workers=21`，DashScope `qwen3.5-flash`，MinerU VLM 抽取。
- Spec：`code/sources/runs/specs/shanghai_2017_autumn_paper_answer.json`。
- 初始页数：题面 4 页，参考答案 9 页。
- 过程记录：首次 `run_pipeline.py` 在 Step4 视觉资产对账中失败，答案第 2 页 `A-V02-P01` 的模型 tool arguments 把 `assets` 返回为字符串。已补强 `prompts/zh_CN/step4_visual_assets.prompt.md` 的工具参数格式约束后重跑。
- Source import：题面 4 页 `extracted`，耗时 12.726 秒；答案 9 页 `extracted`，耗时 22.947 秒；source 汇总耗时 35.673 秒。
- 成功 pipeline：报告耗时 34.964 秒，日志时间戳估算 wall time 35.273 秒。
- Step2：21 题全部对齐，缺答案 0，额外答案 0；题面 crop 26，答案 crop 36。
- Step3：21/21 成功，错误 0，worker 21，耗时 15.221 秒。
- Step3.5：21/21 完成，错误 0；审计发现数从 16 降到 4，worker 21，耗时 16.042 秒。
- Step4：资产 4，风险 1；同步变更 1 题，新增占位 1，移动 0，删除 0，合并答案 0，耗时 3.462 秒。
- Step5：资产引用 4，导出 4，缺失 0；渲染入口 `code/sources/runs/rendered_question_bank_mathjax/shanghai_2017_autumn_paper_answer/index.html`。
- Evidence：`code/sources/runs/reports/shanghai_2017_autumn_paper_answer_v12_pipeline_evidence.json`。

## 2026-06-05 shanghai_2017_autumn_paper_answer Q12 答案图归属修复

- 问题：Step4 将答案页图片 `A-V02-P01` 同步到了 Q12 的 `stem_latex`，导致题干同时出现题面图 `Q-V02-P01` 和答案图 `A-V02-P01`。
- 根因：Step2 对 answer 侧视觉资产给出了冲突归属；连续 answer range 让 `A-V02-P01` 落在 Q11 的 `answer.items.labels`，而 `step2_pure_answer_ranges.json` 又把它列为 Q12 的 `visual_labels`。Step4 compact 因此同时携带 `candidate_qnos=[11]` 和 Q12 visual label 证据。
- 代码修复：`step2_layout.py` 在构建 answer labels 时以 answer range 的视觉资产 `visual_labels` 为最终归属；归属到其他题的视觉资产会从连续区间 labels 中移除，归属到当前题但不在连续区间中的视觉资产会合并回当前题 labels。
- 代码修复：`step4_runtime.py` 增加跨源字段硬校验，`source_part=paper` 只能写入 `stem_latex`/`options_latex`，`source_part=answer` 只能写入 `answer_latex`/`analysis_latex`。
- Prompt 修复：`step4_visual_assets.prompt.md` 明确答案页“如图所示”“建立坐标系”等解析过程配图归入 `analysis_latex`，不得因为与题面图相似而补到 `stem_latex`。
- Rerun：已将本 run spec 的 `cache_policy.force_pipeline` 改为 `true`，用于重跑 Step2-Step5。
- 重跑结果：`run_pipeline.py` 成功，stderr 为空，报告耗时 69.396 秒。
- Step2：21 题全部对齐，缺答案 0，额外答案 0；题面 crop 26，答案 crop 38。`A-V02-P01` 已从 Q11 的 answer labels 移除，并归入 Q12 的 answer labels。
- Step3：21/21 成功，错误 0，worker 21，耗时 14.404 秒。
- Step3.5：21/21 完成，错误 0；审计发现数从 18 降到 4，worker 21，耗时 14.022 秒。
- Step4：资产 4，风险 0，重试 0；同步变更 1 题，新增占位 1，移动 0，删除 0。唯一新增为 `Q-V01-P01 -> Q7 stem_latex`，与 Q12 答案图问题无关。
- Q12 验证：`stem_latex` 保留 `Q-V02-P01` 且不含 `A-V02-P01`；`analysis_latex` 含 `A-V02-P01`，位置在“建立平面直角坐标系，如图所示”附近。
- Step5：资产引用 4，导出 4，缺失 0；渲染入口仍为 `code/sources/runs/rendered_question_bank_mathjax/shanghai_2017_autumn_paper_answer/index.html`。

## 2026-06-05 shanghai_2019_spring_mixed

- 任务：按 `code/sources/runs/specs/shanghai_2019_spring_mixed.json` 直接导入。
- 初始校验失败：spec 文件名和 `sources.mixed_pdf` 指向 2019 春考 mixed 导入，但内部 `run_id` 误写为 `shanghai_2018_autumn_paper_answer`，`input_mode` 误写为 `paper_plus_answer`，导致 `paper_plus_answer requires paper source input`。
- 修正：只将 `run_id` 改为 `shanghai_2019_spring_mixed`，将 `input_mode` 改为 `mixed`；避免污染已存在的 `shanghai_2018_autumn_paper_answer` run。
- Source import：mixed PDF 15 页 `extracted`，耗时 13.016 秒；命令 wall time 13.295 秒。
- Pipeline：`run_pipeline.py` 成功，stderr 为空，报告耗时 82.153 秒。
- Step2：21 题全部对齐，缺答案 0，额外答案 0；mixed crop 40。
- Step3：21/21 成功，错误 0，worker 21，耗时 11.755 秒。
- Step3.5：使用 `runs/call_specs/step35_latex_audit.dashscope.qwen3.7-plus.tool_calling.json`；21/21 完成，错误 0；审计发现数从 31 降到 13，worker 21，耗时 36.11 秒。
- Step4：资产 16，风险 1，重试 0；同步变更 2 题，新增 0，移动 3，删除 3，合并答案 0，耗时 4.985 秒。风险为 `Q-V03-P01` 同时出现在题 8 和题 10 候选列表中，Step4 判定它实际属于题 10。
- Step5：资产引用 15，导出 15，缺失 0；含 1 个表格 HTML，15 个 raster 复制。
- Question bank：`question_bank.json` 21 条，`question_bank.jsonl` 21 行。
- Evidence：`code/sources/runs/reports/shanghai_2019_spring_mixed_v12_pipeline_evidence.json`。
- 渲染入口：`code/sources/runs/rendered_question_bank_mathjax/shanghai_2019_spring_mixed/index.html`。

## 2026-06-05 shanghai_2019_spring_mixed Q18 题面缺失修复

- 问题：Q18 最终 `stem_latex` 只保留“已知数列...”总题干，缺少（1）（2）两个小问。
- 核查：Step2 的 `qa_alignment.json` 已包含 `Q-V09-B17` 和 `Q-V09-B18`；Q18 第一张题面 crop 中两个小问清晰可见，且位于【分析】之前。
- 根因：Step3 只接收图片，不接收 OCR 文本；现有 prompt 对 mixed 解析卷中“同一裁剪图内题面、分析、解答共存”的边界要求不够硬，模型在拆分时漏掉了【分析】前的小问。
- Prompt 修复：`prompts/zh_CN/step3_question_json.prompt.md` 增加 mixed 解析卷边界规则，明确题号之后、【分析】、【解答】、【答案】等解析标记之前的文字均属于题面，并要求（1）（2）等小问条件和作答要求完整保留在 `stem_latex`。
- 契约检查：`python.exe -m exam_import.cli.check_contracts` 通过。
- Rerun：`cache_policy.force_pipeline=false`，复用 Step2 产物，重新生成 Step3-Step5；`run_pipeline.py` 成功，报告耗时 54.327 秒。
- Step3：21/21 成功，错误 0，worker 21，重试 0，耗时 12.241 秒。
- Step3.5：21/21 完成，错误 0，重试 0，耗时 37.15 秒。
- Step4：资产 15，风险 2，重试 0；同步变更 2 题，移动 2，删除 2，新增 0，耗时 4.812 秒。风险为 `Q-V03-P01` 的 Q8/Q10 候选冲突和 `Q-V10-P01` 的 Q19 表格标签确认，均与 Q18 文本无关。
- Step5：资产引用 15，导出 15，缺失 0，耗时 0.063 秒。
- Q18 验证：`stem_latex` 现在包含三段，分别为总题干、（1）“若为等差数列...求 S_n”、（2）“若为等比数列...求公比 q 的取值范围”；`issues` 为空。
- Evidence：`code/sources/runs/reports/shanghai_2019_spring_mixed_v12_pipeline_evidence.json`。
- 渲染入口：`code/sources/runs/rendered_question_bank_mathjax/shanghai_2019_spring_mixed/index.html`。

## 2026-06-05 Step3.5 独立快速审计脚本

- 任务：实现 standalone Step3.5 脚本，支持 full 和 patch 两种模式，不改 `run_pipeline.py` 主流程。
- 新增 CLI：`exam_import/cli/run_step35_audit.py`，参数包含 `--mode full|patch`、`--spec`、`--input-question-bank`、`--output-root`、`--workers`、`--call-spec`、`--write-back`。
- 默认行为：读取 Step3 输入题库，输出到 `runs/reviews_step3_5_latex_audit_standalone/<mode>/<run_id>/`，不回写最终题库；只有 `--write-back` 才备份并更新 `question_bank.json`。
- 新增 patch 配套：`tool_schemas/step35_latex_patch.schema.json`、`prompts/zh_CN/step35_latex_patch.prompt.md`、`call_specs/step35_latex_audit_patch.dashscope.qwen3.7-plus.tool_calling.json`。
- 本地硬校验：patch 只支持字符串 `replace`，path 限定为 `stem_latex`、`answer_latex`、`analysis_latex` 和 `options_latex[].options[].content_latex` 的数组元素；`expected_old_json` 经 JSON 解析后必须逐字匹配旧值；不得增删资产占位标签；应用后必须通过 `QuestionRecord.from_dict`。
- 本地校验：成功覆盖正常 replace、`expected_old_json` 不匹配、非法 path、资产标签变化四类场景。
- 契约检查：`python.exe -m exam_import.cli.check_contracts` 通过；`step35_latex_patch` prompt、schema、call spec 均为 ok。
- Standalone full 对比：输入 `question_bank.before_step3_5_latex_audit.json`，21 题全部调用，耗时 37.656 秒；before finding 27，after finding 13，错误 0，重试 0。
- Standalone patch 对比：同一输入，12 题调用，9 题无 finding 直接透传，耗时 22.056 秒；before finding 27，after finding 13，错误 0，重试 0；patch edit 21 条。
- 效果验证：full 和 patch 的 after finding 分布一致，均为 Q3:1、Q4:1、Q11:3、Q17:4、Q18:3、Q20:1；Q18 题面两个小问均保留。
- 输出目录：full 为 `runs/reviews_step3_5_latex_audit_standalone/full/shanghai_2019_spring_mixed/`，patch 为 `runs/reviews_step3_5_latex_audit_standalone/patch/shanghai_2019_spring_mixed/`。本次测试未使用 `--write-back`，未覆盖最终 question bank。

## 2026-06-05 Step3 qwen3.7-plus 速度与效果测试

- 初始状态：`provider_config/models/qwen3.7-plus.json` 中 `supports_vision=false`，而 Step3 call spec 为 `images=true`，按本地 resolver 会拒绝运行。
- API 探测：直接向 DashScope `qwen3.7-plus` 发送 Q18 crop 图片，返回 200，usage 中包含 `image_tokens=353`，证明当前 API 实际接受图片输入。
- 配置修正：将 `qwen3.7-plus` 的 `supports_vision` 改为 `true`；新增 `call_specs/step3_question_json.dashscope.qwen3.7-plus.tool_calling.json`。
- 契约检查：`python.exe -m exam_import.cli.check_contracts` 通过，新增 Step3 qwen3.7-plus call spec 为 ok。
- 测试方式：复用 `shanghai_2019_spring_mixed` 的 Step2 crop 与 `qa_alignment.json`，standalone 调用 Step3，不写回最终 question bank；输出到 `runs/reviews_step3_model_compare/shanghai_2019_spring_mixed/`。
- qwen3.5-flash：21/21 成功，错误 0，重试 0，worker 21，耗时 13.481 秒；Step3 后本地 audit finding 20，issues 2；Q18 本次漏掉两个小问且 `answer_latex` 为空。
- qwen3.7-plus：21/21 成功，错误 0，重试 0，worker 21，耗时 37.038 秒；Step3 后本地 audit finding 13，issues 1；Q18 保留两个小问。
- 质量观察：qwen3.7-plus 的格式 finding 更少，Q18 内容完整性更好；但它倾向保留“（几分）”评分标记，17 题 stem 中保留“（14分）”，并且 17 题没有从解析中摘取独立答案。按当前 Step3 prompt，“不从解析反推答案”是更严格的边界行为，但评分标记需要后续 prompt 或本地审计另行约束。

## 2026-06-05 Step3 dashscope qwen3.6-27b 速度与效果测试

- API 探测：DashScope 可用模型名为 `qwen3.6-27b`，发送 Q18 crop 图片返回 200 且包含 `image_tokens=353`；`Qwen/Qwen3.6-27B` 在 DashScope 返回 404。现有 `Qwen/Qwen3.6-27B` 配置仅用于 SiliconFlow。
- 配置新增：`provider_config/models/qwen3.6-27b.json` 绑定 DashScope；新增 `call_specs/step3_question_json.dashscope.qwen3.6-27b.tool_calling.json`。
- 契约检查：`python.exe -m exam_import.cli.check_contracts` 通过，新增 DashScope qwen3.6-27b Step3 call spec 为 ok。
- 测试方式：复用 `shanghai_2019_spring_mixed` 的 Step2 crop 与 `qa_alignment.json`，standalone 调用 Step3，不写回最终 question bank；输出到 `runs/reviews_step3_model_compare/shanghai_2019_spring_mixed/qwen3.6-27b/`。
- qwen3.6-27b：21/21 成功，错误 0，重试 0，worker 21，耗时 16.469 秒；Step3 后本地 audit finding 19，issues 1；Q18 保留两个小问。
- 三模型对比：qwen3.5-flash 13.481 秒、finding 20、Q18 漏小问；qwen3.6-27b 16.469 秒、finding 19、Q18 完整；qwen3.7-plus 37.038 秒、finding 13、Q18 完整。
- 质量观察：qwen3.6-27b 速度接近 flash，内容完整性优于本次 flash；但它更倾向保留“（几分）”评分标记，20 题命中评分标记；17-21 题 `answer_latex` 为空，边界更保守；Q19 生成了未在 Step2 映射中的 `Q-V10-F01`，已被本地资产白名单清除并记录 warning。

## 2026-06-05 Step3 allowed_asset_labels 输入修复

- 问题：Step3 之前只通过裁剪图红框让模型“看见”资产标签，没有把本题可用资产标签作为结构化列表提供给模型；模型仍可能发明或误抄非本题标签，之后只能靠本地白名单清洗。
- 代码修复：`Step3Job` 新增 `allowed_asset_labels`；`run_pipeline.py` 从 `qa_alignment` 为每题计算资产标签列表并传入 Step3；题面侧只使用明确视觉归属 `question.visual_labels`，答案侧从 `answer.items.labels` 过滤视觉资产；`sanitize_step3_asset_placeholders` 复用同一标签计算函数作为后置白名单。
- Prompt 修复：`step3_question_json.prompt.md` 增加 `allowed_asset_labels` JSON 输入和资产标签选择规则；要求 `<img>`、`<table>`、`<chart>` 的 `src` 必须从本题列表逐字选择，列表为空时禁止输出资产占位。
- 文档同步：`doc/zh_CN/step3_question_json.md` 已同步新增规则。
