# Step2 qa_alignment 四模式契约

目标：把 `qa_alignment.json` 定义为 Step2 到 Step3、Step4、Step5 的唯一主链路表，覆盖 `pure_paper`、`mixed`、`paper_plus_answer`、后导入 `answer` 四种模式。

## 核心结论

`qa_alignment.json` 不是单纯的“题面-答案配对表”，而是每道顶层题的主索引表：

- 题面范围从这里读。
- 答案来源状态从这里读。
- Step3 裁剪图输入从这里读。
- Step4 资产占位对账的候选题号从这里读。
- Step5 渲染审阅时的 Step2 来源信息从这里读。

不再设计 `question_groups.json` 旁路。Step2 后续只输出 `qa_alignment.json` 和 `pipeline_summary.json`，下游不得从其他题面分组文件补读范围或资产标签。

## 模式命名

`alignment_mode` 使用四种规范值：

| alignment_mode | 题面来源 | 答案来源 | 说明 |
| --- | --- | --- | --- |
| `pure_paper` | paper | 无 | 只有题面，答案状态为待导入。 |
| `mixed` | mixed | mixed | 题面、答案、解析在同一逻辑文档中。 |
| `paper_plus_answer` | paper | answer | 初次导入时已经同时有题面文件和答案文件。 |
| `answer_patch` | 既有 paper | 后导入 answer | 已有纯题面 run 后续补答案；必须保留原题面范围，只更新答案侧。 |

运行入口、配置文件、`pipeline_summary.json` 和 `qa_alignment.json` 均应使用上述规范值，不再保留其他 mode alias。

## 顶层结构

```json
{
  "schema_version": "qa_alignment_v2",
  "run_id": "<run_id>",
  "alignment_mode": "pure_paper|mixed|paper_plus_answer|answer_patch",
  "source_parts": ["paper"],
  "qa_alignment": [],
  "extra_answer_numbers": [],
  "missing_answer_numbers": [],
  "answer_import": {
    "status": "pending|not_applicable|initial_import|patched",
    "preserves_question_side": false,
    "source_run_id": "",
    "answer_pdf": "",
    "patched_at": ""
  },
  "visual_asset_assignment_summary": {
    "asset_count": 0,
    "assigned_count": 0,
    "noise_count": 0,
    "uncertain_count": 0,
    "risk_count": 0
  },
  "asset_match_risk_count": 0
}
```

### 顶层字段

- `schema_version`：固定为 `qa_alignment_v2`。
- `alignment_mode`：四模式规范值。
- `source_parts`：逻辑来源，可为 `paper`、`answer`、`mixed`。
- `qa_alignment`：每道顶层题一行。
- `extra_answer_numbers`：答案侧出现但题面侧不存在的题号。
- `missing_answer_numbers`：题面侧存在但答案侧缺失或未导入的题号。
- `answer_import`：答案导入状态；后导入答案必须写 `patched`。
- `visual_asset_assignment_summary`：Step4 同步后可回写的资产对账摘要。
- `asset_match_risk_count`：资产对账风险数量。

## 行结构

每道顶层题一行。题面侧和答案侧必须分开存储。

```json
{
  "question_no": 1,
  "question": {
    "source_part": "paper",
    "range_mode": "pure_paper",
    "status": "found",
    "labels": ["Q-V01-B01"],
    "core_labels": ["Q-V01-B01"],
    "surface_labels": ["Q-V01-B01"],
    "visual_labels": []
  },
  "answer": {
    "status": "pending_import",
    "source_mode": "none",
    "source_part": "none",
    "embedded_in_question_range": false,
    "items": []
  },
  "alignment_status": "question_only",
  "issues": []
}
```

### 行字段

- `question_no`：顶层题号。
- `question.source_part`：题面逻辑来源，只能是 `paper` 或 `mixed`。
- `question.range_mode`：题面范围检测模式，只能是 `pure_paper` 或 `mixed`。
- `question.labels`：Step3 题面裁剪可使用的标签，包含必要上下文。
- `question.core_labels`：Step2 起止范围内的核心标签。
- `question.surface_labels`：题干、选项等题面文本标签。
- `question.visual_labels`：明显属于该题但不在连续范围内的图、表、图表标签。
- `answer.status`：答案侧状态，可为 `pending_import`、`found`、`missing`、`embedded`。
- `answer.source_mode`：答案来源模式，可为 `none`、`pure_answer`、`mixed`、`answer_patch`。
- `answer.source_part`：答案逻辑来源，可为 `none`、`answer`、`mixed`。
- `answer.embedded_in_question_range`：答案解析是否嵌在题面同一范围内。
- `answer.items`：答案侧独立范围。mixed 模式可为空，因为 Step3 从同一组裁剪图中整理题干、答案和解析。
- `alignment_status`：可为 `question_only`、`question_with_answer`、`mixed_embedded`、`answer_patched`、`needs_review`。
- `issues`：只记录真实问题，不写格式说明。

## 四种模式的写入规则

### pure_paper

- `alignment_mode` 写 `pure_paper`。
- `source_parts` 写 `["paper"]`。
- 每题必须有题面行。
- `question.source_part` 写 `paper`。
- `question.range_mode` 写 `pure_paper`。
- `answer.status` 写 `pending_import`。
- `answer.source_mode` 写 `none`。
- `answer.source_part` 写 `none`。
- `answer.embedded_in_question_range` 写 `false`。
- `answer.items` 写空数组。
- `alignment_status` 写 `question_only`。
- `missing_answer_numbers` 可写全部题号，用于提示当前没有答案侧来源。

### mixed

- `alignment_mode` 写 `mixed`。
- `source_parts` 写 `["mixed"]`。
- 逻辑上题面和答案解析来自同一 mixed 范围。
- `question.source_part` 写 `mixed`。
- `question.range_mode` 写 `mixed`。
- `answer.status` 写 `embedded`。
- `answer.source_mode` 写 `mixed`。
- `answer.source_part` 写 `mixed`。
- `answer.embedded_in_question_range` 写 `true`。
- 如果 Step2 不拆分答案解析内部块，`answer.items` 可以为空。
- `alignment_status` 写 `mixed_embedded`。
- `missing_answer_numbers` 写空数组，不能因为没有独立 `answer.items` 就判定答案缺失。

### paper_plus_answer

- `alignment_mode` 写 `paper_plus_answer`。
- `source_parts` 写 `["paper", "answer"]`。
- 题面侧来自 `pure_paper` 范围。
- 答案侧来自 `pure_answer` 范围。
- 按 `question_no` 对齐答案范围。
- 匹配到答案范围时，`answer.status` 写 `found`，`answer.source_mode` 写 `pure_answer`，`answer.source_part` 写 `answer`，`answer.items` 至少一项。
- 未匹配到答案范围时，`answer.status` 写 `missing`，并把题号写入 `missing_answer_numbers`。
- 答案侧多出的题号写入 `extra_answer_numbers`。
- 有答案时 `alignment_status` 写 `question_with_answer`，缺答案时写 `needs_review`。

### answer_patch

- `alignment_mode` 写 `answer_patch`。
- `source_parts` 写 `["paper", "answer"]`。
- 必须读取既有 `pure_paper` 的 `qa_alignment.json`。
- 不得重跑或重写题面侧范围。
- 只运行答案侧 `pure_answer` 范围检测，并按 `question_no` 回填 `answer`。
- 匹配到答案范围时，`answer.status` 写 `found`，`answer.source_mode` 写 `answer_patch`，`answer.source_part` 写 `answer`，`alignment_status` 写 `answer_patched`。
- 未匹配到答案范围时，`answer.status` 写 `missing`，`alignment_status` 写 `needs_review`。
- `answer_import.status` 写 `patched`。
- `answer_import.preserves_question_side` 写 `true`。

## JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version",
    "run_id",
    "alignment_mode",
    "source_parts",
    "qa_alignment",
    "extra_answer_numbers",
    "missing_answer_numbers",
    "answer_import",
    "visual_asset_assignment_summary",
    "asset_match_risk_count"
  ],
  "properties": {
    "schema_version": {
      "const": "qa_alignment_v2"
    },
    "run_id": {
      "type": "string",
      "minLength": 1
    },
    "alignment_mode": {
      "enum": ["pure_paper", "mixed", "paper_plus_answer", "answer_patch"]
    },
    "source_parts": {
      "type": "array",
      "minItems": 1,
      "uniqueItems": true,
      "items": {
        "enum": ["paper", "answer", "mixed"]
      }
    },
    "qa_alignment": {
      "type": "array",
      "minItems": 1,
      "items": {
        "$ref": "#/$defs/alignment_row"
      }
    },
    "extra_answer_numbers": {
      "type": "array",
      "items": {
        "type": "integer",
        "minimum": 1
      }
    },
    "missing_answer_numbers": {
      "type": "array",
      "items": {
        "type": "integer",
        "minimum": 1
      }
    },
    "answer_import": {
      "$ref": "#/$defs/answer_import"
    },
    "visual_asset_assignment_summary": {
      "$ref": "#/$defs/visual_summary"
    },
    "asset_match_risk_count": {
      "type": "integer",
      "minimum": 0
    }
  },
  "$defs": {
    "string_array": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1
      }
    },
    "issue": {
      "type": "object",
      "additionalProperties": false,
      "required": ["type", "severity", "message"],
      "properties": {
        "type": {
          "type": "string",
          "minLength": 1
        },
        "severity": {
          "enum": ["info", "warning", "error"]
        },
        "message": {
          "type": "string"
        }
      }
    },
    "question_side": {
      "type": "object",
      "additionalProperties": false,
      "required": ["source_part", "range_mode", "status", "labels", "core_labels", "surface_labels", "visual_labels"],
      "properties": {
        "source_part": {
          "enum": ["paper", "mixed"]
        },
        "range_mode": {
          "enum": ["pure_paper", "mixed"]
        },
        "status": {
          "enum": ["found", "missing"]
        },
        "labels": {
          "$ref": "#/$defs/string_array"
        },
        "core_labels": {
          "$ref": "#/$defs/string_array"
        },
        "surface_labels": {
          "$ref": "#/$defs/string_array"
        },
        "visual_labels": {
          "$ref": "#/$defs/string_array"
        }
      }
    },
    "answer_item": {
      "type": "object",
      "additionalProperties": false,
      "required": ["role", "range_mode", "labels", "confidence", "reason"],
      "properties": {
        "role": {
          "enum": ["answer", "analysis", "rubric", "mixed_answer_analysis"]
        },
        "range_mode": {
          "enum": ["pure_answer", "mixed"]
        },
        "labels": {
          "$ref": "#/$defs/string_array"
        },
        "span_text_excerpt": {
          "type": "string"
        },
        "continues_previous": {
          "type": "boolean"
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        },
        "reason": {
          "type": "string"
        }
      }
    },
    "answer_side": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status", "source_mode", "source_part", "embedded_in_question_range", "items"],
      "properties": {
        "status": {
          "enum": ["pending_import", "found", "missing", "embedded"]
        },
        "source_mode": {
          "enum": ["none", "pure_answer", "mixed", "answer_patch"]
        },
        "source_part": {
          "enum": ["none", "answer", "mixed"]
        },
        "embedded_in_question_range": {
          "type": "boolean"
        },
        "items": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/answer_item"
          }
        }
      }
    },
    "alignment_row": {
      "type": "object",
      "additionalProperties": false,
      "required": ["question_no", "question", "answer", "alignment_status", "issues"],
      "properties": {
        "question_no": {
          "type": "integer",
          "minimum": 1
        },
        "question": {
          "$ref": "#/$defs/question_side"
        },
        "answer": {
          "$ref": "#/$defs/answer_side"
        },
        "alignment_status": {
          "enum": ["question_only", "question_with_answer", "mixed_embedded", "answer_patched", "needs_review"]
        },
        "issues": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/issue"
          }
        }
      }
    },
    "answer_import": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status", "preserves_question_side", "source_run_id", "answer_pdf", "patched_at"],
      "properties": {
        "status": {
          "enum": ["pending", "not_applicable", "initial_import", "patched"]
        },
        "preserves_question_side": {
          "type": "boolean"
        },
        "source_run_id": {
          "type": "string"
        },
        "answer_pdf": {
          "type": "string"
        },
        "patched_at": {
          "type": "string"
        }
      }
    },
    "visual_summary": {
      "type": "object",
      "additionalProperties": false,
      "required": ["asset_count", "assigned_count", "noise_count", "uncertain_count", "risk_count"],
      "properties": {
        "asset_count": {
          "type": "integer",
          "minimum": 0
        },
        "assigned_count": {
          "type": "integer",
          "minimum": 0
        },
        "noise_count": {
          "type": "integer",
          "minimum": 0
        },
        "uncertain_count": {
          "type": "integer",
          "minimum": 0
        },
        "risk_count": {
          "type": "integer",
          "minimum": 0
        }
      }
    }
  }
}
```

## 构建与更新算法

1. 先构建题面侧行。`pure_paper`、`paper_plus_answer`、`answer_patch` 使用 `pure_paper` 范围；`mixed` 使用 `mixed` 范围。
2. 再构建答案侧状态。`pure_paper` 标记待导入；`mixed` 标记嵌入同范围；`paper_plus_answer` 和 `answer_patch` 使用 `pure_answer` 范围按题号对齐。
3. `answer_patch` 不得修改既有题面字段，只允许写入 `answer`、`missing_answer_numbers`、`extra_answer_numbers` 和 `answer_import`。
4. 下游 Step3、Step4、Step5 只读取 `qa_alignment.json`。
5. 若发现题面范围、答案范围或资产标签需要调整，应回到对应步骤重跑，不手工补写最终 JSON。
