# Step4 Assets Prompt

来源代码：`step4_asset_distribution.py`

Step4 有两个大模型任务：

- 单页视觉资产占位对账：检查 Step3 已输出的图片、图表、表格占位标签是否与版面资产一致，只处理缺失或误归属的边界情况。
- 答案页表格抽取：识别答案速查表，抽取可直接合并到 Step3 `answer_markdown` 的逐题最终答案。

兼容关系：

- Step4 输入必须沿用 Step2 的 `range_mode` 语义：`pure_paper`、`pure_answer`、`mixed`。
- Step4 的题号候选来自 Step2 `question_ranges`，不得额外发明题号。
- Step4 的字段目标必须与 Step3 审定结构一致，只能指向 `stem_markdown`、`options_markdown`、`answer_markdown`、`analysis_markdown` 或 `none`。
- Step4 的图片、表格、图表标签必须与 Step3 占位标签共用同一 label 空间，占位形式只能是 `<img src="...">`、`<table src="...">`、`<chart src="...">`。
- Step3 已经负责在题干、选项、答案、解析中放置资产占位；Step4 不做全面重写，只对账并标记两类边界：资产属于某字段但 Step3 未放占位、资产不属于某字段但 Step3 已放占位。
- Step4 可以覆盖所有图片、表格、图表，但完整遍历必须由本地代码先枚举到 `assets[]`；模型只处理输入中已经列出的资产，不能补发现未输入的图片。

## 1. 视觉资产占位对账

### 输入消息结构

`messages`：

```json
[
  {
    "role": "system",
    "content": "<system_prompt>"
  },
  {
    "role": "user",
    "content": [
      {"type": "text", "text": "第 <page> 页标注图："},
      {"type": "image_url", "image_url": {"url": "<annotated_page_data_uri>"}},
      {"type": "text", "text": "<instruction_with_compact_payload>"}
    ]
  }
]
```

`compact` payload：

```json
{
  "run_id": "<run_id>",
  "range_mode": "pure_paper|pure_answer|mixed",
  "source_part": "paper|answer|mixed",
  "page": 1,
  "step2_question_ranges": [
    {
      "question_no": 17,
      "start_label": "Q-V08-B01",
      "end_label": "Q-V08-B09",
      "visual_labels": ["Q-V08-P01"]
    }
  ],
  "step3_records": [
    {
      "schema_version": "image_only_question_standardization_v1",
      "question_no": 17,
      "stem_markdown": ["如图，<img src=\"Q-V08-P01\">"],
      "options_markdown": [],
      "answer_markdown": [],
      "analysis_markdown": [],
      "issues": []
    }
  ],
  "assets": [
    {
      "label": "Q-V08-P01",
      "kind": "image|table|chart",
      "candidate_qnos": [17],
      "candidate_fields": ["stem_markdown"],
      "asset_part": "paper|answer|mixed",
      "step2_relation": "in_range|visual_label|outside_nearest|orphan",
      "step2_visual_label": true,
      "step3_placeholder_refs": [
        {
          "question_no": 17,
          "field": "stem_markdown",
          "option_group_no": "",
          "option_label": "",
          "placeholder": "<img src=\"Q-V08-P01\">"
        }
      ]
    }
  ]
}
```

### system prompt

#### function_calling

```text
/no_think

你是中文数学试卷视觉资产占位对账工具。
本次任务必须通过调用工具 submit_step4_visual_assets 完成。
不得在普通回复正文中写 JSON、Markdown、代码块、解释或分析过程。
不得使用 message.content 提交结果。

你只核对 Step3 已输出的图片、表格、图表占位是否与标注页资产一致，并标记需要新增、删除或移动占位的边界情况。
不得解题，不得把图片内容描述成题库正文，不得新增题号，不得重写 Step3 正文。
```

### user prompt

```text
输入是一页带框标注的中文数学试卷图片，以及本页图片、表格、图表资产的紧凑上下文。
请对每个资产做 Step3 占位对账，判断现有占位是否正确，或是否存在缺失、误归属、字段错误。

规则：
- 每个输入 assets[] 项必须输出一次。
- 一个资产只能有一个最终归属；不得把同一个 label 同时归到多个题号或多个字段。
- 如果同一个资产在 Step3 多处出现，占位对账必须选择唯一正确最终位置；其他位置视为重复或误放占位，由同步阶段根据 step3_placeholder_refs 删除。
- Step3 已经负责把资产占位写入字段；Step4 默认只确认或纠错，不要重新组织正文。
- 除 target_field 为 none 外，question_no 必须来自该资产的 candidate_qnos。
- target_field 只能填写 stem_markdown、options_markdown、answer_markdown、analysis_markdown、none。
- pure_paper 或 source_part=paper 时，target_field 只能是 stem_markdown、options_markdown、none。
- pure_answer 或 source_part=answer 时，target_field 只能是 answer_markdown、analysis_markdown、none。
- mixed 时，必须根据资产所在上下文判断目标字段。
- candidate_fields 非空时，target_field 必须来自 candidate_fields，除非输出 none 并说明原因。
- 如果资产已经在 step3_records 的占位标签中出现，且字段、题号和语义均正确，placeholder_status 填 matched，action 填 keep_existing。
- 如果资产确实属于某个 Step3 字段，但 step3_placeholder_refs 为空，或没有出现在正确字段，placeholder_status 填 belongs_but_missing_placeholder，action 填 add_placeholder。
- 如果资产已经出现在某个 Step3 字段，但从标注页和上下文判断不属于该题或该字段，placeholder_status 填 placeholder_but_not_belong，action 填 remove_placeholder 或 move_placeholder。
- 如果资产属于同一题但字段错误，例如放在 stem_markdown 实际应在 analysis_markdown，placeholder_status 填 wrong_field，action 填 move_placeholder。
- 如果 Step3 中已有占位，但仅是标签类型错误，例如应为 <chart src="..."> 却写成 <img src="...">，placeholder_status 填 wrong_tag，action 填 move_placeholder，并给出正确 placeholder。
- 如果资产不在 Step2 的 start_label/end_label 连续范围内，但 step2_relation 为 visual_label 或 outside_nearest，且能确认属于某题某字段，不扩大 Step2 范围，insert_position 填 append_to_field_end；若 target_field=options_markdown，则填 append_to_option_end。
- kind=image 时 asset_tag 填 img，placeholder 形如 <img src="Q-V08-P01">。
- kind=table 时 asset_tag 填 table，placeholder 形如 <table src="M-V02-T01">。
- kind=chart 时 asset_tag 填 chart，placeholder 形如 <chart src="M-V02-C01">。
- 如果上游仍使用 figure 表示图表，输出时按 chart 处理。
- 选项中的图片、表格或图表必须填写 target_field=options_markdown，并填写 option_label；能判断选项组时填写 option_group_no，否则填空字符串。
- 题干中的图片、表格或图表填写 target_field=stem_markdown。
- 答案行、答案表中的图片、表格或图表填写 target_field=answer_markdown。
- 解析、证明、计算过程、评分说明中的图片、表格或图表填写 target_field=analysis_markdown。
- 二维码、水印、广告、装饰图或无关资产填写 target_field=none、asset_tag=none、placeholder 为空字符串。
- 无法安全判断时 placeholder_status 填 uncertain，action 填 review_required，target_field 填 none，并在 risks 中记录。
- caption_labels 只能填写附近作为图题、表题的文本块标签。
- caption_text 只复制图题或表题原文，不要描述资产内容。

{output_rule}

紧凑上下文：
{compact_json}

{final_rule}
```

### 输出格式

工具名：`submit_step4_visual_assets`

百炼 Function Calling 工具定义：

```json
{
  "type": "function",
  "function": {
    "name": "submit_step4_visual_assets",
    "description": "提交 Step4 单页视觉资产占位对账结果，输出 Step3 占位确认、新增、删除或移动建议。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "assets",
        "risks"
      ],
      "properties": {
        "assets": {
          "type": "array",
          "description": "每个输入 asset 的归属结果。每个输入 asset 必须出现一次。",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "label",
              "question_no",
              "placeholder_status",
              "action",
              "source_field",
              "target_field",
              "insert_position",
              "asset_tag",
              "placeholder",
              "option_group_no",
              "option_label",
              "caption_labels",
              "caption_text",
              "confidence",
              "reason"
            ],
            "properties": {
              "label": {
                "type": "string",
                "description": "输入资产 label。"
              },
              "question_no": {
                "type": "integer",
                "description": "归属题号；target_field 为 none 且无具体题号时填写 0。"
              },
              "placeholder_status": {
                "type": "string",
                "enum": [
                  "matched",
                  "belongs_but_missing_placeholder",
                  "placeholder_but_not_belong",
                  "wrong_field",
                  "wrong_tag",
                  "no_placeholder_needed",
                  "uncertain"
                ],
                "description": "Step3 占位与实际资产关系的对账状态。"
              },
              "action": {
                "type": "string",
                "enum": [
                  "keep_existing",
                  "add_placeholder",
                  "remove_placeholder",
                  "move_placeholder",
                  "ignore_asset",
                  "review_required"
                ],
                "description": "同步阶段应采取的动作建议。"
              },
              "source_field": {
                "type": "string",
                "enum": [
                  "stem_markdown",
                  "options_markdown",
                  "answer_markdown",
                  "analysis_markdown",
                  "none"
                ],
                "description": "Step3 当前已有占位所在字段；没有已有占位时填写 none。"
              },
              "target_field": {
                "type": "string",
                "enum": [
                  "stem_markdown",
                  "options_markdown",
                  "answer_markdown",
                  "analysis_markdown",
                  "none"
                ],
                "description": "该资产最终应当位于 Step3 的哪个字段；不应保留时填写 none。"
              },
              "insert_position": {
                "type": "string",
                "enum": [
                  "existing_position",
                  "append_to_field_end",
                  "append_to_option_end",
                  "remove_existing",
                  "none"
                ],
                "description": "同步阶段的占位位置策略。范围外新增或移动时追加到目标字段末尾；选项资产追加到对应选项末尾。"
              },
              "asset_tag": {
                "type": "string",
                "enum": [
                  "img",
                  "table",
                  "chart",
                  "none"
                ],
                "description": "该资产在 Step3 字段中使用的占位标签类型。"
              },
              "placeholder": {
                "type": "string",
                "description": "最终应保留或新增的完整占位标签，例如 <img src=\"Q-V08-P01\">、<table src=\"M-V02-T01\">、<chart src=\"M-V02-C01\">；target_field 为 none 时为空字符串。"
              },
              "option_group_no": {
                "type": "string",
                "description": "target_field 为 options_markdown 时填写对应 <options no=\"...\"> 的 no；无法判断或非选项资产时为空字符串。"
              },
              "option_label": {
                "type": "string",
                "description": "target_field 为 options_markdown 时填写原文选项标识，例如 A/B/C/D；否则填写空字符串。"
              },
              "caption_labels": {
                "type": "array",
                "description": "附近作为图题或表题的文本块标签；没有则为空数组。",
                "items": {
                  "type": "string"
                }
              },
              "caption_text": {
                "type": "string",
                "description": "只复制图题或表题原文；没有则为空字符串。"
              },
              "confidence": {
                "type": "number",
                "description": "归属判断置信度，0 到 1。"
              },
              "reason": {
                "type": "string",
                "description": "简短中文理由。"
              }
            }
          }
        },
        "risks": {
          "type": "array",
          "description": "资产归属风险；没有则为空数组。",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "label",
              "severity",
              "reason"
            ],
            "properties": {
              "label": {
                "type": "string"
              },
              "severity": {
                "type": "string",
                "enum": [
                  "info",
                  "warning",
                  "error"
                ]
              },
              "reason": {
                "type": "string",
                "description": "简短中文风险说明。"
              }
            }
          }
        }
      }
    }
  }
}
```

百炼请求中应强制调用该工具：

```json
{
  "enable_thinking": false,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "submit_step4_visual_assets",
        "description": "提交 Step4 单页视觉资产占位对账结果，输出 Step3 占位确认、新增、删除或移动建议。",
        "parameters": "<上方 parameters JSON Schema>"
      }
    }
  ],
  "tool_choice": {
    "type": "function",
    "function": {
      "name": "submit_step4_visual_assets"
    }
  },
  "parallel_tool_calls": false
}
```

约束：

- 百炼返回结果必须从 `message.tool_calls[0].function.arguments` 读取。
- 如果没有 `tool_calls`，流程必须失败。
- 每个输入 asset 必须在输出 `assets` 中出现。
- 每个输入 asset label 在输出 `assets` 中必须出现且只出现一次；不得输出同一 label 的多条归属。
- 除 `target_field=none` 外，`question_no` 必须来自 `candidate_qnos`。
- `target_field` 必须与 Step3 字段集合一致。
- `placeholder` 必须是 Step3 允许的占位标签，且 `src` 必须等于输入资产 `label`。
- `target_field=options_markdown` 时必须填写 `option_label`。
- `target_field=none` 时 `asset_tag` 必须为 `none`，`placeholder` 必须为空字符串。
- `placeholder_status=matched` 时 `action` 必须为 `keep_existing`，`source_field` 和 `target_field` 应一致。
- `placeholder_status=belongs_but_missing_placeholder` 时 `action` 必须为 `add_placeholder`，`source_field` 必须为 `none`，`target_field` 不得为 `none`。
- `placeholder_status=placeholder_but_not_belong` 时 `action` 必须为 `remove_placeholder` 或 `move_placeholder`。
- `placeholder_status=wrong_field` 或 `wrong_tag` 时 `action` 必须为 `move_placeholder`。
- `action=remove_placeholder` 或 `ignore_asset` 时 `target_field` 必须为 `none`。
- `action=keep_existing` 时 `insert_position` 必须为 `existing_position`。
- `action=add_placeholder` 或 `move_placeholder` 时，如果输入 `step2_relation` 为 `visual_label` 或 `outside_nearest`，`insert_position` 必须为 `append_to_field_end`；当 `target_field=options_markdown` 时必须为 `append_to_option_end`。
- `action=remove_placeholder` 时 `insert_position` 必须为 `remove_existing`。
- `ignore_asset` 或 `review_required` 时 `insert_position` 必须为 `none`。
- 工具参数返回后仍必须由本地 schema 和业务规则再校验，不得直接信任模型输出。

## 2. 答案页表格抽取

### 输入消息结构

`messages`：

```json
[
  {
    "role": "system",
    "content": "<system_prompt>"
  },
  {
    "role": "user",
    "content": [
      {"type": "text", "text": "第 <page> 页标注图："},
      {"type": "image_url", "image_url": {"url": "<annotated_page_data_uri>"}},
      {"type": "text", "text": "<instruction_with_compact_payload>"}
    ]
  }
]
```

`compact` payload：

```json
{
  "run_id": "<run_id>",
  "range_mode": "pure_answer|mixed",
  "source_part": "answer",
  "page": 1,
  "step2_question_ranges": [
    {
      "question_no": 13,
      "start_label": "A-V01-B20",
      "end_label": "A-V01-B22",
      "visual_labels": []
    }
  ],
  "step3_records": [
    {
      "schema_version": "image_only_question_standardization_v1",
      "question_no": 13,
      "stem_markdown": [],
      "options_markdown": [],
      "answer_markdown": [],
      "analysis_markdown": [],
      "issues": []
    }
  ],
  "tables": [
    {
      "label": "A-V01-B22",
      "html": "<table>...</table>",
      "candidate_qnos": [13, 14, 15]
    }
  ]
}
```

### system prompt

#### function_calling

```text
/no_think

你是中文数学试卷答案页表格分类和最终答案抽取工具。
本次任务必须通过调用工具 submit_step4_answer_tables 完成。
不得在普通回复正文中写 JSON、Markdown、代码块、解释或分析过程。
不得使用 message.content 提交结果。

你只判断答案页表格是否为答案速查表，并抽取表格中直接可见的逐题最终答案。
不得解题，不得推断表格中没有直接给出的答案。
```

### user prompt

```text
输入是一页带框标注的中文数学试卷答案页图片，以及表格块的 OCR HTML。
请分类每个表格；只有当表格是答案速查表时，才抽取逐题最终答案。

规则：
- 每个输入 tables[] 项必须输出一次。
- 不得解题，不得推断表格中没有直接给出的答案。
- 如果表格把题号映射到选择字母、填空结果或最终答案，role 使用 answer_key_table，target_field 使用 answer_markdown。
- answer_key_table 的 entries 可以非空；其他 role 的 entries 必须为空数组。
- 如果表格是解答过程、证明过程、评分说明或点评表，role 使用 analysis_table，target_field 使用 analysis_markdown，但 entries 必须为空数组。
- 如果表格是页眉页脚、装饰、二维码、水印、广告或无关内容，role 使用 noise，target_field 使用 none。
- 如果无法安全判断，role 使用 uncertain，target_field 使用 none，并在 risks 中记录。
- entries[].question_no 必须来自表格可见题号，通常应位于 candidate_qnos 中。
- entries[].answer_markdown 是可直接合并到 Step3 answer_markdown 的 JSON 字符串数组。
- 选择题字母答案必须写成 LaTeX 行内格式，例如 $A$。
- 数学表达式必须用 LaTeX。行内数学用 $...$，展示数学用 $$...$$。
- 不得把解析表、评分表或过程表中的中间结果抽成最终答案。

{output_rule}

紧凑上下文：
{compact_json}

{final_rule}
```

### 输出格式

工具名：`submit_step4_answer_tables`

百炼 Function Calling 工具定义：

```json
{
  "type": "function",
  "function": {
    "name": "submit_step4_answer_tables",
    "description": "提交 Step4 答案页表格分类和最终答案抽取结果，答案条目可直接合并到 Step3 answer_markdown。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "tables",
        "risks"
      ],
      "properties": {
        "tables": {
          "type": "array",
          "description": "每个输入 table 的分类和答案条目。每个输入 table 必须出现一次。",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "label",
              "role",
              "target_field",
              "entries",
              "confidence",
              "reason"
            ],
            "properties": {
              "label": {
                "type": "string",
                "description": "输入表格 label。"
              },
              "role": {
                "type": "string",
                "enum": [
                  "answer_key_table",
                  "analysis_table",
                  "noise",
                  "uncertain"
                ]
              },
              "target_field": {
                "type": "string",
                "enum": [
                  "answer_markdown",
                  "analysis_markdown",
                  "none"
                ],
                "description": "该表格抽取结果应当合并到 Step3 的哪个字段。"
              },
              "entries": {
                "type": "array",
                "description": "逐题最终答案条目。只有 role 为 answer_key_table 时可以非空。",
                "items": {
                  "type": "object",
                  "additionalProperties": false,
                  "required": [
                    "question_no",
                    "answer_markdown",
                    "confidence",
                    "reason"
                  ],
                  "properties": {
                    "question_no": {
                      "type": "integer"
                    },
                    "answer_markdown": {
                      "type": "array",
                      "description": "可直接合并到对应 Step3 记录 answer_markdown 的答案段落数组。",
                      "items": {
                        "type": "string",
                        "minLength": 1
                      }
                    },
                    "confidence": {
                      "type": "number",
                      "description": "答案抽取置信度，0 到 1。"
                    },
                    "reason": {
                      "type": "string",
                      "description": "简短中文理由。"
                    }
                  }
                }
              },
              "confidence": {
                "type": "number",
                "description": "表格分类置信度，0 到 1。"
              },
              "reason": {
                "type": "string",
                "description": "简短中文理由。"
              }
            }
          }
        },
        "risks": {
          "type": "array",
          "description": "答案表格分类或抽取风险；没有则为空数组。",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "label",
              "severity",
              "reason"
            ],
            "properties": {
              "label": {
                "type": "string"
              },
              "severity": {
                "type": "string",
                "enum": [
                  "info",
                  "warning",
                  "error"
                ]
              },
              "reason": {
                "type": "string",
                "description": "简短中文风险说明。"
              }
            }
          }
        }
      }
    }
  }
}
```

百炼请求中应强制调用该工具：

```json
{
  "enable_thinking": false,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "submit_step4_answer_tables",
        "description": "提交 Step4 答案页表格分类和最终答案抽取结果，答案条目可直接合并到 Step3 answer_markdown。",
        "parameters": "<上方 parameters JSON Schema>"
      }
    }
  ],
  "tool_choice": {
    "type": "function",
    "function": {
      "name": "submit_step4_answer_tables"
    }
  },
  "parallel_tool_calls": false
}
```

约束：

- 百炼返回结果必须从 `message.tool_calls[0].function.arguments` 读取。
- 如果没有 `tool_calls`，流程必须失败。
- 每个输入 table 必须在输出 `tables` 中出现。
- 只有 `answer_key_table` 可以输出非空 `entries`。
- `answer_key_table` 的 `target_field` 必须是 `answer_markdown`。
- `analysis_table` 的 `target_field` 必须是 `analysis_markdown`，但 `entries` 必须为空数组。
- `noise` 和 `uncertain` 的 `target_field` 必须是 `none`，且 `entries` 必须为空数组。
- `entries[].question_no` 必须来自表格可见题号，通常位于 `candidate_qnos`。
- `entries[].answer_markdown` 必须满足 Step3 `answer_markdown` 的格式要求。
- 不得从解析表或评分表中推断最终答案。
- 工具参数返回后仍必须由本地 schema 和业务规则再校验，不得直接信任模型输出。
