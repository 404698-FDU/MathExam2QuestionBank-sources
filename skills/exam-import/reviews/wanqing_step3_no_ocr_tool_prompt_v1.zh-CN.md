# 万擎 Step3 Image-only Tool Calling Prompt 草案 v1

用途：给 `step3_question2json.py --no-ocr-input --structured-output tool_calling` 使用。  
目标模型：快手万擎 `qwen3.6-35b-a3b`。  
设计目标：减少模型把 JSON 写进 `message.content` 的概率，强制它通过 tool call 参数提交结果。

## 1. 当前问题判断

当前 Step3 prompt 里有多处类似表达：

- `输出必须是满足 JSON Schema 的单个 JSON object`
- `只返回严格 JSON`
- `只输出 JSON object，不要 Markdown`
- `输入 JSON：`

这些表达对支持强约束 tool calling 的平台通常没问题，但对万擎这次测试会产生副作用：模型经常把完整 JSON 放到 `message.content`，没有返回 `tool_calls`。

所以 v1 草案做三件事：

- prompt 中不再出现“返回 JSON”“输出 JSON object”“JSON Schema”这类正文输出指令。
- 所有结构化提交都表述为“调用工具并填写工具参数”。
- `--no-ocr-input` 下只描述图片输入，不提 OCR 字段，不给 JSON 示例。

## 2. 请求体格式

建议 Step3 image-only 万擎请求使用：

```json
{
  "model": "qwen3.6-35b-a3b",
  "messages": [
    {
      "role": "system",
      "content": "<见第 3 节 SYSTEM_PROMPT>"
    },
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "<见第 4 节 USER_TEXT>"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "<题面裁剪图 data URI>"
          }
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "<答案/解析裁剪图 data URI>"
          }
        }
      ]
    }
  ],
  "temperature": 0,
  "top_p": 0.8,
  "enable_thinking": false,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "submit_step3_image_only_record",
        "description": "提交从题面图和答案解析图整理出的单题题库记录。",
        "parameters": "<见第 5 节 TOOL_SCHEMA>"
      }
    }
  ],
  "tool_choice": {
    "type": "function",
    "function": {
      "name": "submit_step3_image_only_record"
    }
  }
}
```

注意：

- `tool_choice` 必须指定函数名。
- 不使用正文 JSON 示例。
- 不在 user prompt 末尾写“现在返回 JSON”。
- 如果平台仍返回 `message.content`，应视为 provider/tool 执行不稳定，而不是本地 schema 成功。

## 3. SYSTEM_PROMPT

```text
/no_think

你是中文数学试题题库整理器。

本次任务必须通过调用工具 submit_step3_image_only_record 完成。
不得在普通回复正文中写 JSON、Markdown、代码块、解释或分析过程。
不得使用 message.content 提交结果。

你只根据用户提供的题面裁剪图和答案/解析裁剪图整理指定题号。
不得解题，不得补充条件，不得改题意，不得凭常识修补图片中没有出现的内容。
如果图片中看不清、边界不确定、答案未独立给出，必须在工具参数 issues 中记录。
```

## 4. USER_TEXT 模板

```text
任务：整理第 {question_no} 题。

输入方式：image_only_no_ocr。
本次不提供 OCR 文本。题面、选项、答案和解析均以图片为准。

图片顺序：
1. 题面裁剪图：包含第 {question_no} 题的题干、选项、表格和必要图形。图片边缘可能含相邻题残留，只整理第 {question_no} 题。
2. 答案/解析裁剪图：包含第 {question_no} 题的答案、解析、证明过程或评分信息。图片边缘可能含相邻题残留，只整理第 {question_no} 题。

必须调用工具 submit_step3_image_only_record，并在工具参数中填写整理结果。
不要在正文中输出任何内容。

字段填写规则：

1. question_no
- 必须等于 {question_no}。

2. question_type
- 只能填写 fill_blank、single_choice、multiple_choice、solution、unknown。
- 有 A/B/C/D 选项且只有一个答案：single_choice。
- 有 A/B/C/D 选项且多个答案：multiple_choice。
- 有明确填空横线、空格、待填内容且无选项：fill_blank。
- 要求证明、求解、解答，且需要完整过程：solution。
- 图片残缺到无法判断：unknown。

3. stem_latex
- 填正式题干和小问。
- 不写题号。
- 不把答案填回题干。
- 填空位置写 <blank>，选择题作答位置写 <choice_blank>。
- 数学表达式必须用 LaTeX。行内数学用 $...$，展示数学用 $$...$$。

4. options_latex
- 必须包含 A、B、C、D 四个键。
- 只写选项正文，不写 A.、B.、(A)、(B) 标签。
- 非选择题四个键都填空数组。
- 如果某个选项只有图，没有文字，该选项填空数组，图片归属交给后续步骤。

5. answer_latex
- 只填写答案图中明确独立给出的最终答案、答案行或各小题答案。
- 不要从解析过程中反推答案。
- 数学答案必须用 $...$。
- 如果没有独立答案行，填空数组，并在 issues 记录 answer_missing。

6. analysis_latex
- 填答案/解析图中的解答步骤、证明过程、计算过程、思路分析、点评。
- 不要摘要，不要改写，不要删减。
- 如果解析看不清，保留能确认的内容，并在 issues 记录 ocr_unclear。

7. rubric_latex
- 只填写独立成段或独立成表的评分标准、扣分说明、阅卷规则。
- 没有独立评分标准时填空数组。

8. issues
- 没有问题时填空数组。
- 只记录真实问题，不写格式说明。
- 可用 type：image_unclear、boundary_suspect、answer_missing、type_conflict、content_missing、foreign_content、other。
- severity 只能是 info、warning、error。

9. LaTeX 规则
- 每个数组元素是一段，不是真实 OCR 行。
- 每个数组元素必须是单行字符串，不得包含换行。
- 不要输出空字符串数组项。
- LaTeX 命令不能拆开，例如 \frac、\sqrt、\leq、\in 必须完整。
- <blank> 和 <choice_blank> 只能在数学环境外。

再次强调：必须调用工具 submit_step3_image_only_record。普通正文保持为空。
```

## 5. TOOL_SCHEMA

这个 schema 故意保持扁平，避免 `$defs`，也避免复杂 `oneOf/anyOf`。

```json
{
  "type": "object",
  "required": [
    "schema_version",
    "question_no",
    "question_type",
    "stem_latex",
    "options_latex",
    "answer_latex",
    "analysis_latex",
    "rubric_latex",
    "issues"
  ],
  "properties": {
    "schema_version": {
      "type": "string",
      "enum": ["step3_json_schema_v1"]
    },
    "question_no": {
      "type": "integer"
    },
    "question_type": {
      "type": "string",
      "enum": ["fill_blank", "single_choice", "multiple_choice", "solution", "unknown"]
    },
    "stem_latex": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "options_latex": {
      "type": "object",
      "required": ["A", "B", "C", "D"],
      "properties": {
        "A": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "B": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "C": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "D": {
          "type": "array",
          "items": {
            "type": "string"
          }
        }
      },
      "additionalProperties": false
    },
    "answer_latex": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "analysis_latex": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "rubric_latex": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "issues": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["type", "severity", "message"],
        "properties": {
          "type": {
            "type": "string",
            "enum": [
              "image_unclear",
              "boundary_suspect",
              "answer_missing",
              "type_conflict",
              "content_missing",
              "foreign_content",
              "other"
            ]
          },
          "severity": {
            "type": "string",
            "enum": ["info", "warning", "error"]
          },
          "message": {
            "type": "string"
          }
        },
        "additionalProperties": false
      }
    }
  },
  "additionalProperties": false
}
```

## 6. 与当前实现的主要差异

### 6.1 不再混用 OCR prompt

当前 `--no-ocr-input` 只是 payload 去掉 OCR，但仍使用通用 OCR prompt。v1 草案改成 image-only 专用 prompt。

### 6.2 不再写“返回 JSON”

当前 prompt 的“返回严格 JSON”会诱导万擎输出正文 JSON。v1 草案只写“调用工具提交参数”。

### 6.3 工具函数名区分 image-only

当前函数名是 `submit_step3_record`。v1 草案用 `submit_step3_image_only_record`，让模型明确这是视觉输入任务。

### 6.4 issue type 改为 image_unclear

`--no-ocr` 下没有 OCR，因此 `ocr_unclear` 不准确。v1 草案改用 `image_unclear`。

## 7. 建议小测矩阵

先不要直接整卷跑。建议用万擎做小测：

| 测试 | 题号 | 输入 | 目标 |
|---|---:|---|---|
| A | Q1 | image-only | 是否返回 tool_calls |
| B | Q12 | image-only | 大图/图形题是否返回 tool_calls |
| C | Q16 | image-only | 是否仍产生空字符串数组项 |
| D | Q21 | image-only | 长解答题是否返回 tool_calls |

每题跑 3 次，统计：

- `tool_calls` 成功次数
- Pydantic 校验成功次数
- 空字符串数组项次数
- `answer_latex` 为空次数
- 裸 LaTeX 或未包 `$...$` 次数

如果 v1 仍频繁返回正文 JSON，则基本可以判断是万擎 tool calling 多模态实现不够强约束，后续应考虑 `content JSON + 本地 schema 校验` 路线，而不是继续打磨 prompt。
