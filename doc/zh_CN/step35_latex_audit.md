# Step3.5 Full LaTeX Normalize Prompt

来源代码：`step35_normalize.py`

任务：对 Step3 输出的每道单题题库 JSON 做全量格式审计和规范化修复。Step3.5 full 模式不是只修 `audit_findings` 命中的局部文本；即使 `audit_findings` 为空，也必须完整审计所有文字字段。Step3.5 只修格式，不解题、不改题意、不补内容、不重新分类字段。

兼容关系：

- 输入 `record` 必须是 Step3 输出的 `image_only_question_standardization_v1` 结构。
- 输出字段必须与 Step3 完全一致：`schema_version`、`question_no`、`stem_latex`、`options_latex`、`answer_latex`、`analysis_latex`、`issues`。
- 不保留旧版 `question_type`、`stem_latex`、`options_latex`、`answer_latex`、`analysis_latex`、`rubric_latex` 字段。
- Step3.5 不产生新题目内容，只对 Step3 已输出内容做 JSON 字符串、LaTeX 包裹、LaTeX 定界、HTML 实体、占位标签和字段一致性修复。
- `audit_findings` 只是脚本审计提示，不是修复范围上限；模型必须对完整记录做全量检查。

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
    "content": "<user_prompt_with_record_and_audit_findings_json>"
  }
]
```

`record_and_audit_findings_json`：

```json
{
  "record": {
    "schema_version": "image_only_question_standardization_v1",
    "question_no": 1,
    "stem_latex": [],
    "options_latex": [
      {
        "no": "1",
        "options": [
          {
            "label": "A",
            "content_latex": []
          }
        ]
      }
    ],
    "answer_latex": [],
    "analysis_latex": [],
    "issues": []
  },
    "audit_findings": [
    {
      "field": "stem_latex",
      "path": "stem_latex[0]",
      "reason": "math_condition_split_across_text_connector",
      "text": "<problem_text>"
    }
  ]
}
```

## 本地 audit_findings 规则

`audit_findings` 由 `step35_normalize.py` 在发送模型前生成。Step3.5 仍会把每道题完整送入模型；这些 finding 只用于提示重点问题，不是修复范围上限。

逐段文本审计覆盖 `stem_latex`、`options_latex[].options[].content_latex`、`answer_latex`、`analysis_latex`：

- `html_entity_present`：字段中含 `&gt;`、`&lt;`、`&amp;`、`&nbsp;`、数字实体等 HTML 实体。
- `embedded_newline`：数组元素中包含真实换行。
- `math_condition_split_across_text_connector`：相邻数学片段被“且/或/并且”等中文连接词拆开。
- `left_right_count_mismatch`：数学环境内 `\left` 与 `\right` 数量不一致。
- `blank_inside_math`：`<blank>` 位于 `$...$` 或 `$$...$$` 内。
- `choice_blank_inside_math`：`<choice_blank>` 位于 `$...$` 或 `$$...$$` 内。
- `asset_placeholder_inside_math`：`<img src="...">`、`<table src="...">`、`<chart src="...">` 位于数学环境内。
- `latex_command_outside_math`：普通文本段中出现 `\frac`、`\sqrt`、`\leq` 等 LaTeX 命令。
- `math_relation_outside_math`：普通文本段中出现类似 `a < b`、`x=1`、`t \geq 2` 的数学关系。
- `subsup_outside_math`：普通文本段中出现未进入数学环境的上下标。
- `unknown_placeholder_tag`：字段中出现白名单之外的尖括号标签。

整题级审计覆盖选项题占位：

- `options_group_reference_mismatch`：`stem_latex` 中的 `<options no="...">` 与 `options_latex` 分组不一致。
- `missing_choice_blank`：`options_latex` 非空，但题干没有 `<choice_blank>`。
- `duplicate_choice_blank`：`options_latex` 非空，且题干中出现多个 `<choice_blank>`。
- `raw_choice_blank_not_normalized`：`options_latex` 非空，且题干仍保留 `（）` 或 `( )` 这类原始选择题空位。
- `choice_blank_in_options`：选项内容中出现 `<choice_blank>`。

## system prompt

### function_calling

```text
/no_think

你是中文数学题库 LaTeX 全量规范化工具。
本次任务必须通过调用工具 submit_step3_5_record 完成。
不得在普通回复正文中写 JSON、Markdown、代码块、解释或分析过程。
不得使用 message.content 提交结果。

你必须完整审计输入记录中的所有文字字段：
- stem_latex
- options_latex[].options[].content_latex
- answer_latex
- analysis_latex

你只修复 Step3 单题记录中的格式问题，包括 JSON 字符串合法性、LaTeX 包裹、LaTeX 定界、LaTeX 转义、HTML 实体、占位标签位置和选项组引用一致性。
不得解题、不得改写题意、不得增删图片中没有出现的内容、不得新增题目类型字段、不得把解析内容改写成答案。
```

## user prompt

```text
下面是一道 Step3 已经结构化好的中文数学题记录，以及脚本审计出的格式提示。
请对整道题做完整文字审计，并输出完整规范化后的单题记录。

任务边界：
- 必须完整检查 stem_latex、options_latex[].options[].content_latex、answer_latex、analysis_latex，不得只处理 audit_findings 指出的局部文本。
- audit_findings 只是重点提示；即使 audit_findings 为空，也要全量审计所有文字字段。
- 只在原字段内修复格式，除 issues 中明显已经不成立的格式类提示外，不改变字段含义。
- 输出字段必须与 Step3 审定结构完全一致，不得输出 question_type、rubric_latex 或任何旧版字段。
- schema_version 必须保持为 image_only_question_standardization_v1。
- question_no 必须保持不变。
- stem_latex、answer_latex、analysis_latex 都必须是 JSON 字符串数组。
- options_latex 必须是选项组数组；每个选项组包含 no 和 options；每个选项包含 label 和 content_latex。
- content_latex 必须是 JSON 字符串数组。
- 所有数组元素都必须是单行 JSON 字符串，禁止包含真实换行或 \n。
- 不要输出空字符串数组项。

LaTeX 修复规则：
- 数学表达式必须放入 $...$，展示数学放入 $$...$$。
- 中文说明文字一般放在数学环境外；数学环境内需要中文连接词时，用 \text{...}。
- 不要把完整中文句子整段放进数学环境，除非它本来就是展示公式的一部分。
- 如果一整段选项几乎全是数学条件，例如 0 < a < 1 且 x > 1/2，则把整段包成一个行内数学块，并把中文连接词写成 \text{ 且 }、\text{ 或 } 等。
- 如果审计原因包含 math_condition_split_across_text_connector，说明一个集合、条件或分段数学表达被拆成了 `$...$ 中文连接词 `$...$`。请把相邻数学片段和连接词合并为一个数学表达式，连接词写入 `\text{...}`。
- 合并这类片段时只修复 LaTeX 分段，不得增删原有数学条件；只有为配平括号、定界符或 JSON 转义所必需时，才可以调整 `\left`、`\right`。
- 如果发现 \left 与 \right 不配平，必须配平或移除不必要的 \left、\right。集合的条件分隔符使用 \mid，绝对值可以写成 |...| 或完整配对的 \left|...\right|。
- 如果只有局部数学，例如中文句子中的变量、公式、区间，则只包局部数学，不要把整句中文都放入数学环境。
- LaTeX 命令不能拆开，例如 \frac、\sqrt、\leq、\in 必须完整。
- 已经正确的数学表达式不要重复包裹。
- 不得改变原有数学符号写法本身。例如原文是 $N^*$ 就保持 $N^*$，不得改成 $\mathbf{N}^*$ 或 $\mathbb{N}^*$；原文是 $\mathbf{N}^*$ 也不得改成 $N^*$。
- 工具参数是 JSON 字符串；所有 LaTeX 反斜杠必须按 JSON 字符串规则正确转义。
- 字段中不得保留 HTML 实体，例如 &gt;、&lt;、&amp;。修复时只替换为等价字符，例如 &gt; 改为 >，&lt; 改为 <，&amp; 改为 &。

占位标签规则：
- <blank> 和 <choice_blank> 必须保留在数学环境外。
- <blank> 表示填空题作答位置。如果原文用空括号、横线或空白表示填空，应统一为 <blank>。
- 如果填空位置紧接公式末尾，应写成 `$公式 = $ <blank>` 或 `$公式$ <blank>`，不要写成 `$公式 = <blank>$`。
- 选择题题干必须且只能有一个 <choice_blank>。
- 如果原文作答位是 `（）`、`( )`、空括号或类似选择题空位，应统一为 <choice_blank>。
- <choice_blank> 不应放到选项内；它属于题干作答位置。
- 只允许使用这些占位标签：<blank>、<choice_blank>、<options no="...">、<img src="...">、<table src="...">、<chart src="...">。
- 只要 options_latex 非空，stem_latex 就必须显式包含对应的 `<options no="...">` 占位，不允许依赖渲染阶段自动补出选项。
- stem_latex 中出现的 `<options no="...">` 或 `<options no="..."/>`，都必须在 options_latex 中有且仅有一个同 no 的选项组；反过来，options_latex 中的每个选项组也都必须在 stem_latex 中被显式引用一次。
- 图片、表格、图表占位标签不得放入数学环境。

内容边界：
- 不得从 analysis_latex 反推或摘取答案到 answer_latex。
- 不得把 answer_latex 中的最终答案扩写为解析。
- 不得删除或改写原有中文、英文、数字和标点。
- issues 字段保留原有含义；除非只是删除已经明显不再成立的格式类提示，否则不要新增业务判断。
- 如果只能确认局部内容，保留能确认的内容，并在 issues 中记录真实问题。
- issues 没有问题时输出空数组；不得写格式说明。

必须调用工具 submit_step3_5_record，提交完整规范化后的单题题库 JSON 记录。
不要在普通正文中输出任何内容。

输入 JSON：
{record_and_audit_findings_json}
```

## 输出格式

工具名：`submit_step3_5_record`

百炼 Function Calling 工具定义：

```json
{
  "type": "function",
  "function": {
    "name": "submit_step3_5_record",
    "description": "提交 Step3.5 full 完整规范化后的单题题库 JSON 记录，字段与 Step3 image-only 标准化结构完全一致。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "schema_version",
        "question_no",
        "stem_latex",
        "options_latex",
        "answer_latex",
        "analysis_latex",
        "issues"
      ],
      "properties": {
        "schema_version": {
          "type": "string",
          "enum": [
            "image_only_question_standardization_v1"
          ]
        },
        "question_no": {
          "type": "integer",
          "description": "必须与输入记录题号一致。"
        },
        "stem_latex": {
          "type": "array",
          "description": "题面正文段落数组，不含题号。可包含 <blank>、<choice_blank>、<options no=\"1\">、<img src=\"...\">、<table src=\"...\">、<chart src=\"...\">。",
          "items": {
            "type": "string",
            "minLength": 1
          }
        },
        "options_latex": {
          "type": "array",
          "description": "按 stem_latex 中 <options no=\"...\"> 的顺序输出选项组。",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "no",
              "options"
            ],
            "properties": {
              "no": {
                "type": "string",
                "minLength": 1
              },
              "options": {
                "type": "array",
                "minItems": 1,
                "items": {
                  "type": "object",
                  "additionalProperties": false,
                  "required": [
                    "label",
                    "content_latex"
                  ],
                  "properties": {
                    "label": {
                      "type": "string",
                      "description": "原文选项标识，例如 A、B、C、D、甲、乙、①、②。"
                    },
                    "content_latex": {
                      "type": "array",
                      "items": {
                        "type": "string",
                        "minLength": 1
                      }
                    }
                  }
                }
              }
            }
          }
        },
        "answer_latex": {
          "type": "array",
          "description": "明确独立给出的最终答案、答案行、答案表或各小题答案；没有则为空数组。",
          "items": {
            "type": "string",
            "minLength": 1
          }
        },
        "analysis_latex": {
          "type": "array",
          "description": "答案/解析图中实际可见的完整解析内容；没有则为空数组。",
          "items": {
            "type": "string",
            "minLength": 1
          }
        },
        "issues": {
          "type": "array",
          "description": "真实问题列表；没有则为空数组。",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "type",
              "severity",
              "message"
            ],
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
                "enum": [
                  "info",
                  "warning",
                  "error"
                ]
              },
              "message": {
                "type": "string",
                "minLength": 1
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
  "temperature": 0.0,
  "top_p": 0.8,
  "enable_thinking": false,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "submit_step3_5_record",
        "description": "提交 Step3.5 full 完整规范化后的单题题库 JSON 记录，字段与 Step3 image-only 标准化结构完全一致。",
        "parameters": "<上方 parameters JSON Schema>"
      }
    }
  ],
  "tool_choice": {
    "type": "function",
    "function": {
      "name": "submit_step3_5_record"
    }
  },
  "parallel_tool_calls": false
}
```

约束：

- 百炼返回结果必须从 `message.tool_calls[0].function.arguments` 读取。
- 如果没有 `tool_calls`，流程必须失败。
- 返回结果必须再次通过 Step3 `image_only_question_standardization_v1` schema 校验。
- Step3.5 full 必须对每道输入题输出完整记录；不得因为 `audit_findings` 为空而跳过字段审计。
- 后处理仍应执行 before/after audit 和内容指纹保护；格式修复不得造成题意、答案、解析或可见文字语义变化。
- 返回结果不得包含旧版 `question_type`、`stem_latex`、`options_latex`、`answer_latex`、`analysis_latex`、`rubric_latex` 字段。
- Step3.5 不得替代 Step3 的内容转写和字段归类。

