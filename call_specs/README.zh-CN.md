# v12 Call Spec 示例

本目录保存 v12 的单次模型调用配置示例。每个文件都是独立 `call_spec_v1` JSON，可直接被：

- `exam_import/cli/run_pipeline.py`
- `exam_import/cli/answer_patch.py`
- `exam_import/llm/call_spec_loader.py`

读取。

## 作用

这一层只定义“某一步怎么调用某个 provider 的某个 model”，不负责：

- provider 级 token / endpoint 规则
- model 静态能力配置
- prompt 正文
- tool schema 主体

这些分别由：

- `provider_config/providers/*.json`
- `provider_config/models/*.json`
- `prompts/zh_CN/*.prompt.md`
- `tool_schemas/*.schema.json`

负责。

## 当前示例

当前提供的是 DashScope 兼容模式示例，默认每一步使用 `qwen3.5-flash`：

- `step2_question_ranges.dashscope.qwen3.5-flash.tool_calling.json`
- `step3_question_json.dashscope.qwen3.5-flash.tool_calling.json`
- `step35_latex_audit.dashscope.qwen3.5-flash.tool_calling.json`
- `step4_visual_assets.dashscope.qwen3.5-flash.tool_calling.json`
- `step4_answer_tables.dashscope.qwen3.5-flash.tool_calling.json`

## provider 与 model 约束

当前 v12 运行时允许：

- `qwen-vl-max`：`dashscope`
- `qwen3.5-flash`：`dashscope`
- `qwen3.6-plus`：`dashscope`
- `qwen3.7-plus`：`dashscope`
- `Qwen/Qwen3.6-27B`：`siliconflow`
- `gpt-4.1-mini`：`openai`

前提是该 model 在 `provider_config/models/*.json` 中允许该 provider。

对于百炼 / DashScope 的 OpenAI 兼容 Chat：

- 当前 Step2-Step4 统一走 `tool_choice={"type":"function","function":{"name":"..."}}`，即强制指定工具名。
- 官方文档说明，思考模式模型不支持这种“强制指定某个工具”的方式，因此现有百炼 tool-calling call spec 均固定 `enable_thinking=false`。

## import_spec 引用方式

```json
{
  "llm": {
    "provider": "dashscope",
    "primary_model": "qwen3.5-flash",
    "call_specs": {
      "step2_question_ranges": "call_specs/step2_question_ranges.dashscope.qwen3.5-flash.tool_calling.json",
      "step3_question_json": "call_specs/step3_question_json.dashscope.qwen3.5-flash.tool_calling.json",
      "step35_latex_audit": "call_specs/step35_latex_audit.dashscope.qwen3.5-flash.tool_calling.json",
      "step4_visual_assets": "call_specs/step4_visual_assets.dashscope.qwen3.5-flash.tool_calling.json",
      "step4_answer_tables": "call_specs/step4_answer_tables.dashscope.qwen3.5-flash.tool_calling.json"
    }
  }
}
```
