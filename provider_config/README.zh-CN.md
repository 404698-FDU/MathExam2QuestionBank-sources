# v12 Provider Config

本目录保存 v12 runtime 的静态 provider/model 配置。

分层：

- `providers/*.json`：厂商级接入规则，例如 base URL、chat path、token 来源、structured output 能力。
- `models/*.json`：模型级静态能力，例如上下文窗口、视觉支持、支持的 structured output 模式、建议 TPM。

当前约定：

- `dashscope` 与 `bailian` 在 OpenAI 兼容 Chat/Completions 场景下共用同一套百炼兼容端点。
- 两者保留为两个逻辑 provider alias，目的是让 call spec 能明确表达“按 DashScope 直连”还是“按百炼命名习惯”。
- 百炼官方文档里的示例 API Key 环境变量名是 `DASHSCOPE_API_KEY`；本仓库保留 `BAILIAN_API_KEY` 兼容读取只是一层本地别名，不改变实际接入平台。

约束：

- 每个 JSON 文件只定义一个 provider 或一个 model。
- 文件名只用于落盘管理，不作为运行时主键；真正的主键是 JSON 内部的 `name`。
- `call_specs/*.json` 只引用这里已经定义过的 `provider` 和 `model` 名称。
- `check_contracts.py` 会校验本目录存在且可被运行时代码解析。
- 对百炼/DashScope，若使用 `tool_choice={"type":"function",...}` 强制指定某个工具，必须同时关闭 thinking；对应约束已落在 provider config 中。
- `qwen3.7-plus` 已通过 Step3.5 `tool_calling` 实测，当前按可用于文本规范化工具调用配置；视觉输入仍不启用。
