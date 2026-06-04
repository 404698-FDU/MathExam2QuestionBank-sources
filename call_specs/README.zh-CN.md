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

当前提供的是百炼兼容模式示例：

- `step2_question_ranges.bailian.qwen-vl-max.tool_calling.json`
- `step3_question_json.bailian.qwen-vl-max.tool_calling.json`
- `step35_latex_audit.bailian.qwen3.5-flash.tool_calling.json`
- `step4_visual_assets.bailian.qwen-vl-max.tool_calling.json`
- `step4_answer_tables.bailian.qwen-vl-max.tool_calling.json`

## provider 与 model 约束

当前 v12 运行时允许：

- `qwen-vl-max`：`dashscope`、`bailian`
- `qwen3.5-flash`：`dashscope`、`bailian`
- `qwen3.6-plus`：`dashscope`、`bailian`
- `qwen3.7-plus`：`dashscope`、`bailian`
- `Qwen/Qwen3.6-27B`：`siliconflow`
- `gpt-4.1-mini`：`openai`

如果要改成 DashScope 直连版本，通常只需要把示例中的：

```json
"provider": "bailian"
```

改成：

```json
"provider": "dashscope"
```

前提是该 model 在 `provider_config/models/*.json` 中允许该 provider。

对于百炼 / DashScope 的 OpenAI 兼容 Chat：

- 当前 Step2-Step4 统一走 `tool_choice={"type":"function","function":{"name":"..."}}`，即强制指定工具名。
- 官方文档说明，思考模式模型不支持这种“强制指定某个工具”的方式，因此现有百炼 tool-calling call spec 均固定 `enable_thinking=false`。

## import_spec 引用方式

```json
{
  "llm": {
    "provider": "bailian",
    "primary_model": "qwen-vl-max",
    "call_specs": {
      "step2_question_ranges": "assets/v12_runtime/call_specs/step2_question_ranges.bailian.qwen-vl-max.tool_calling.json",
      "step3_question_json": "assets/v12_runtime/call_specs/step3_question_json.bailian.qwen-vl-max.tool_calling.json",
      "step35_latex_audit": "assets/v12_runtime/call_specs/step35_latex_audit.bailian.qwen3.5-flash.tool_calling.json",
      "step4_visual_assets": "assets/v12_runtime/call_specs/step4_visual_assets.bailian.qwen-vl-max.tool_calling.json",
      "step4_answer_tables": "assets/v12_runtime/call_specs/step4_answer_tables.bailian.qwen-vl-max.tool_calling.json"
    }
  }
}
```
