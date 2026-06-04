# Step2 Layout Prompt

来源代码：`step2_layout.py`

任务：让 VLM/OCR 模型识别试卷 OCR 页面的顶层题号范围，输出每道题的起止 OCR block label，以及额外视觉资产 label。

## 输入消息结构

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
  "pages": [
    {
      "page": 1,
      "annotated_page_image": "<path>",
      "blocks": [
        {
          "label": "Q-V01-B01",
          "kind": "text|image|table",
          "text": "<ocr_text>",
          "bbox": [0, 0, 0, 0]
        }
      ]
    }
  ]
}
```

## system prompt

### function_calling

```text
你是试卷 OCR 顶层题号块范围检测器。本次任务必须通过调用工具 submit_question_ranges 完成，不得在普通回复正文中输出结果。
```

## user instruction template

```text
你是试卷 OCR 版面块范围检测器，只识别顶层题号对应的块范围。
{mode_rule}

任务：
- 对每个可见的顶层题号，输出起始标签和结束标签。
- 不要在范围检测阶段区分题干块和答案块。
- 不要分类图、表或图片；如果某个图、表或图片明显属于该题但因跨栏、页首浮动等版面原因不在 start_label 到 end_label 的连续范围内，只把它的标签填入 visual_labels。
- 不得解题、改写、推断缺失题目或修正 OCR。
- 顶层题号形如 1.、2.、21.；(1)(2) 这类小问不是新题。
- 21-I、21-II、21-Ⅰ、21-Ⅱ 这类带连字符或罗马数字的编号是同一顶层题号的分部，不是新的顶层题；必须合并到题号 21 的同一个范围。
- 在答案页或解析页中，每道题的答案、解析、解答过程、评分标准，从该题第一个可见题号或解答标记开始，连续抄到下一道顶层题号出现之前为止；下一题题号及其后内容是排他边界，不得归入上一题。
- 相邻题目的范围允许重叠，尤其是题号块、选项块或跨栏版面边界不确定时，可以让前后两题共用边界标签；但 reason 中必须说明这是边界上下文重叠。
- 在答案页模式下，页首或正文中的紧凑横排答案键也必须按题号拆分；例如“1. ... 2. ... 3. ...”、“10. ... 11. ...”或“二、15. B 16. C”均不是噪声。每个可见顶层题号都要输出一个 question_range；如果多个题号位于同一个 OCR 块，允许这些题复用同一个 start_label 和 end_label，并在 reason 中说明该块包含紧凑答案键。
- 如果一个 OCR 块同时包含上一题尾部和下一题开头，允许相邻两个范围共用该块，作为上一题的结束标签和下一题的起始标签。
- 如果一道题跨页续写，且后续页面没有新的顶层题号，则结束标签延伸到最后一个明显属于该题的续写块。
- 忽略页眉、页脚、大题标题、页码、二维码、水印和装饰块。

{output_rule}

输入：
{compact_json}

{final_rule}
```

`mode_rule`：

```text
pure_paper：当前输入是试题页。每个范围是一道完整顶层题，包含题干、选项、图表和续写块。
pure_answer：当前输入是答案页或解析页。每个范围是一道题完整的答案、解析、解答过程或评分标准。
mixed：当前输入中题干和答案解析交错出现。每个范围是一道完整顶层题，包含题干、选项、图表、答案、解析、评分标准和续写块。
```

## 输出格式

工具名：`submit_question_ranges`

百炼 Function Calling 工具定义：

```json
{
  "type": "function",
  "function": {
    "name": "submit_question_ranges",
    "description": "提交试卷 OCR 顶层题号块范围检测结果。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "question_ranges",
        "noise_blocks",
        "risks"
      ],
      "properties": {
        "question_ranges": {
          "type": "array",
          "description": "每道可见顶层题号对应的 OCR block 起止范围。不得为空。",
          "minItems": 1,
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "question_no",
              "start_label",
              "end_label",
              "visual_labels",
              "confidence",
              "reason"
            ],
            "properties": {
              "question_no": {
                "type": "integer",
                "description": "顶层题号，例如 1、2、21。"
              },
              "start_label": {
                "type": "string",
                "description": "本题范围的起始 OCR block label。"
              },
              "end_label": {
                "type": "string",
                "description": "本题范围的结束 OCR block label。"
              },
              "visual_labels": {
                "type": "array",
                "description": "明显属于本题但不在连续 start/end 范围内的图片、表格或图表标签；没有则为空数组。",
                "items": {
                  "type": "string"
                }
              },
              "confidence": {
                "type": "number",
                "description": "范围判断置信度，0 到 1。"
              },
              "reason": {
                "type": "string",
                "description": "简短中文理由，说明范围边界依据或重叠原因。"
              }
            }
          }
        },
        "noise_blocks": {
          "type": "array",
          "description": "页眉、页脚、大题标题、页码、二维码、水印、装饰块等噪声 block label；没有则为空数组。",
          "items": {
            "type": "string"
          }
        },
        "risks": {
          "type": "array",
          "description": "边界、缺失或不确定风险；没有则为空数组。",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "type",
              "question_no",
              "block_labels",
              "severity",
              "evidence"
            ],
            "properties": {
              "type": {
                "type": "string",
                "enum": [
                  "boundary",
                  "missing",
                  "uncertain"
                ]
              },
              "question_no": {
                "type": "integer",
                "description": "关联题号；如果风险不对应具体题号，填写 0。"
              },
              "block_labels": {
                "type": "array",
                "description": "与风险相关的 block label。",
                "items": {
                  "type": "string"
                }
              },
              "severity": {
                "type": "string",
                "enum": [
                  "info",
                  "warning",
                  "error"
                ]
              },
              "evidence": {
                "type": "string",
                "description": "简短中文证据说明。"
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
        "name": "submit_question_ranges",
        "description": "提交试卷 OCR 顶层题号块范围检测结果。",
        "parameters": "<上方 parameters JSON Schema>"
      }
    }
  ],
  "tool_choice": {
    "type": "function",
    "function": {
      "name": "submit_question_ranges"
    }
  },
  "parallel_tool_calls": false
}
```

约束：

- 百炼返回结果必须从 `message.tool_calls[0].function.arguments` 读取。
- 如果没有 `tool_calls`，流程必须失败。
- `question_ranges` 不得为空；为空时流程失败。
- `noise_blocks` 和 `risks` 没有内容时必须输出空数组。
- 工具参数返回后仍必须由本地 schema 再校验，不得直接信任模型输出。
