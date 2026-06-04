# 从零重做 MathExam2QuestionBank 的 Python 学习 Guide Book

面向读者：Python 初学者  
目标：通过重新实现一个简化版“数学试卷转题库”项目，系统练习文件处理、JSON、图片、OCR/LLM API、数据管线、HTML 渲染、测试与工程组织。  
建议节奏：不要一开始复刻当前完整项目。先做一个能跑通的小系统，再逐步替换为更强的实现。

## 0. 你最终要做出什么

最终项目可以叫：

```text
mini_math_exam_bank/
```

它完成这条链路：

```text
PDF 或图片
  -> OCR 文本和页面图片
  -> 按题号切分
  -> 每题生成标准 JSON
  -> 检查 JSON 和 LaTeX
  -> 整理图片资产
  -> 渲染成 HTML 审核页
```

初学者版本不需要一开始使用 MinerU、复杂版面检测、多模型并发和 TPM 池。第一版只要能处理你手工准备好的 OCR 文本即可。

## 1. 学习路线总览

建议分 8 个阶段：

1. 项目骨架和命令行
2. 数据模型和 JSON 文件
3. OCR 文本读取与清洗
4. 简单按题号切分
5. 调用 LLM 生成单题 JSON
6. 校验和审计
7. HTML + MathJax 渲染
8. 图片、并发、缓存、重试和工程化

每个阶段都要做到：

- 有一个能运行的命令。
- 有输入样例。
- 有输出目录。
- 有 summary。
- 有最少测试。

## 2. 推荐目录结构

第一版目录：

```text
mini_math_exam_bank/
  README.md
  pyproject.toml
  .env.example
  data/
    samples/
      2010_ocr.txt
  runs/
  src/
    mini_exam_bank/
      __init__.py
      cli.py
      io_utils.py
      models.py
      step1_source.py
      step2_split.py
      step3_llm.py
      step4_audit.py
      step5_render.py
      pipeline.py
  tests/
    test_split.py
    test_models.py
    test_audit.py
```

先不要创建太多抽象。每个 step 一个文件，能读懂最重要。

## 3. 环境准备

安装 Python 3.11 或 3.12。

创建虚拟环境：

```powershell
python.exe -m venv .venv
.\.venv\Scripts\Activate.ps1
python.exe -m pip install --upgrade pip
```

推荐依赖：

```text
pydantic
requests
python-dotenv
jinja2
pytest
ruff
```

后续图片/PDF 阶段再加：

```text
pillow
pypdf
pdfplumber
```

## 4. 第 1 阶段：命令行骨架

目标：写出一个可以运行的 CLI。

你需要支持：

```powershell
python.exe -m mini_exam_bank.cli step2-split --input data\samples\2010_ocr.txt --run-id demo_2010
python.exe -m mini_exam_bank.cli step3-llm --run-id demo_2010
python.exe -m mini_exam_bank.cli render --run-id demo_2010
python.exe -m mini_exam_bank.cli pipeline --input data\samples\2010_ocr.txt --run-id demo_2010
```

建议使用标准库 `argparse`，不要一开始上 Typer/Click。

你要练习：

- `argparse.ArgumentParser`
- 子命令 subparsers
- `Path`
- 返回 exit code

验收标准：

- 命令能打印参数。
- 参数缺失时会报错。
- 输出目录 `runs/<run_id>/` 能被创建。

## 5. 第 2 阶段：数据模型

目标：定义一题的标准 JSON。

使用 Pydantic：

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    severity: Literal["info", "warning", "error"]
    message: str


class OptionsLatex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    A: list[str]
    B: list[str]
    C: list[str]
    D: list[str]


class QuestionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["mini_question_v1"]
    question_no: int
    question_type: Literal["fill_blank", "single_choice", "multiple_choice", "solution", "unknown"]
    stem_latex: list[str]
    options_latex: OptionsLatex
    answer_latex: list[str]
    analysis_latex: list[str]
    issues: list[Issue]
```

你要练习：

- 类型标注
- Pydantic 校验
- JSON 读写
- 单元测试

验收标准：

- 合法 JSON 能通过校验。
- 多一个字段会失败。
- 错误题型会失败。

## 6. 第 3 阶段：文件读写工具

写 `io_utils.py`：

功能：

- `read_text(path: Path) -> str`
- `write_text(path: Path, text: str) -> None`
- `read_json(path: Path) -> Any`
- `write_json(path: Path, data: Any) -> None`
- `write_jsonl(path: Path, rows: list[dict]) -> None`
- `ensure_dir(path: Path) -> None`

要求：

- 全部使用 UTF-8。
- JSON 使用 `ensure_ascii=False`。
- 写文件前创建父目录。

验收标准：

- 中文不乱码。
- 输出 JSON 格式缩进可读。

## 7. 第 4 阶段：简单 Step2 切题

第一版不要做复杂版面检测。假设 OCR 文本长这样：

```text
1. 已知集合 A=...
答案：...
解析：...

2. 函数 f(x)=...
答案：...
解析：...
```

实现：

```text
step2_split.py
```

输入：

```text
data/samples/2010_ocr.txt
```

输出：

```text
runs/<run_id>/step2/
  questions.json
  summary.json
```

`questions.json` 示例：

```json
[
  {
    "question_no": 1,
    "raw_text": "1. 已知集合 A=...\n答案：...\n解析：..."
  }
]
```

实现方法：

- 用正则找到行首题号：`^\s*(\d+)[.．、]`
- 每个题号开始到下一个题号前，是一道题。
- 不要追求完美，先跑通。

你要练习：

- 正则表达式
- 字符串切片
- 列表和字典
- summary 统计

验收标准：

- 能切出 5 道样例题。
- summary 包含 `question_count`。
- 题号顺序正确。

## 8. 第 5 阶段：不用 LLM，先做规则版 Step3

在调用 LLM 前，先写一个假 Step3。

输入：

```text
runs/<run_id>/step2/questions.json
```

输出：

```text
runs/<run_id>/question_bank/
  question_bank.json
  summary.json
  per_question/q01.json
```

规则：

- 题干：从开头到 `答案：` 前。
- 答案：`答案：` 到 `解析：` 之间。
- 解析：`解析：` 后面。
- 暂时全部题型写 `unknown`。

这样做的价值：

- 你先把 pipeline 文件结构跑通。
- 后面 LLM 只是替换“字段抽取器”，不是重写整个系统。

验收标准：

- 每题都有 JSON。
- Pydantic 校验通过。
- `question_bank.json` 是所有题的数组。

## 9. 第 6 阶段：接入 LLM

目标：让模型把 `raw_text` 转成标准 JSON。

先实现最简单版本：

```python
import os
import requests


def call_chat(messages: list[dict], model: str, timeout: int = 120) -> str:
    endpoint = os.environ["LLM_ENDPOINT"]
    api_key = os.environ["LLM_API_KEY"]
    response = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": messages,
            "temperature": 0,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]
```

第一步只做普通 JSON 输出。prompt 要求模型输出 JSON。

然后再升级为 tool calling。

## 10. 第 7 阶段：实现 tool calling

为什么要学 tool calling：

- 普通 JSON 输出容易多字段、少字段、类型错。
- 很多模型不支持 `json_schema`，但支持 tool calling。
- tool calling 的 `function.arguments` 更接近受控结构。

请求结构大概是：

```python
payload = {
    "model": model,
    "messages": messages,
    "temperature": 0,
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "submit_question",
                "description": "提交整理后的单题 JSON。",
                "parameters": {
                    "type": "object",
                    "required": [
                        "schema_version",
                        "question_no",
                        "question_type",
                        "stem_latex",
                        "options_latex",
                        "answer_latex",
                        "analysis_latex",
                        "issues"
                    ],
                    "properties": {
                        "schema_version": {"type": "string", "enum": ["mini_question_v1"]},
                        "question_no": {"type": "integer"},
                        "question_type": {
                            "type": "string",
                            "enum": ["fill_blank", "single_choice", "multiple_choice", "solution", "unknown"]
                        },
                        "stem_latex": {"type": "array", "items": {"type": "string"}},
                        "options_latex": {
                            "type": "object",
                            "required": ["A", "B", "C", "D"],
                            "properties": {
                                "A": {"type": "array", "items": {"type": "string"}},
                                "B": {"type": "array", "items": {"type": "string"}},
                                "C": {"type": "array", "items": {"type": "string"}},
                                "D": {"type": "array", "items": {"type": "string"}}
                            }
                        },
                        "answer_latex": {"type": "array", "items": {"type": "string"}},
                        "analysis_latex": {"type": "array", "items": {"type": "string"}},
                        "issues": {"type": "array"}
                    }
                }
            }
        }
    ],
    "tool_choice": {
        "type": "function",
        "function": {"name": "submit_question"}
    }
}
```

解析响应：

```python
tool_calls = data["choices"][0]["message"].get("tool_calls") or []
if not tool_calls:
    raise RuntimeError("model did not call submit_question")

arguments = tool_calls[0]["function"]["arguments"]
record_dict = json.loads(arguments)
record = QuestionRecord.model_validate(record_dict)
```

验收标准：

- 模型必须返回 tool call。
- arguments 必须能 `json.loads`。
- Pydantic 必须通过。
- 每题保存 `qXX_raw.json.txt` 和 `qXX.json`。

## 11. 第 8 阶段：审计规则

不要相信模型输出。你要写 `step4_audit.py`。

第一批审计规则：

- `stem_latex` 为空：error。
- `answer_latex` 为空：warning。
- 题号不一致：error。
- 选择题没有选项：warning。
- 出现裸 LaTeX 命令：warning。

裸 LaTeX 命令示例：

```text
由 d = \frac{...}{...} = 3
```

如果 `\frac` 出现在 `$...$` 外，就要提示。

初学者可以先写一个简单函数：

```python
def has_latex_command_outside_math(text: str) -> bool:
    in_math = False
    i = 0
    while i < len(text):
        if text[i] == "$":
            in_math = not in_math
        if not in_math and text.startswith("\\frac", i):
            return True
        if not in_math and text.startswith("\\sqrt", i):
            return True
        i += 1
    return False
```

这个函数不完美，但足够练习。

验收标准：

- 能抓住 Q5 这类裸 `\frac` 问题。
- 审计结果写入 `issues`。
- summary 统计 warning/error 数量。

## 12. 第 9 阶段：HTML 渲染

目标：生成一个能在浏览器查看的题库页面。

使用：

- Jinja2
- MathJax
- 普通 CSS

输出：

```text
runs/<run_id>/rendered/
  index.html
```

HTML 基本结构：

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <script>
    window.MathJax = { tex: { inlineMath: [["$", "$"], ["\\(", "\\)"]] } };
  </script>
  <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>
</head>
<body>
  <h1>题库审核</h1>
  <!-- questions -->
</body>
</html>
```

每题显示：

- 题号
- 题型
- 题干
- 选项
- 答案
- 解析
- issues

验收标准：

- `$x^2$` 能渲染。
- 裸 `\frac` 会保持文本，从而暴露问题。
- 页面能直接打开。

## 13. 第 10 阶段：加入图片

第一版图片不要自动从 PDF 裁。你可以手工放：

```text
runs/<run_id>/assets/q05_question.png
runs/<run_id>/assets/q05_answer.png
```

在 `questions.json` 中加：

```json
{
  "question_no": 5,
  "raw_text": "...",
  "question_images": ["assets/q05_question.png"],
  "answer_images": ["assets/q05_answer.png"]
}
```

然后让 Step3 LLM 支持图片输入。

你要练习：

- 图片转 base64 data URI
- OpenAI compatible multimodal message
- HTML 图片渲染

验收标准：

- 渲染页能显示题面图片。
- LLM 可以同时收到文本和图片。
- 可以做 image-only 测试。

## 14. 第 11 阶段：完整 pipeline

写 `pipeline.py` 串联：

```python
def run_pipeline(input_path: Path, run_id: str, model: str) -> None:
    run_step2_split(input_path, run_id)
    run_step3_llm(run_id, model)
    run_step4_audit(run_id)
    run_step5_render(run_id)
```

CLI：

```powershell
python.exe -m mini_exam_bank.cli pipeline `
  --input data\samples\2010_ocr.txt `
  --run-id demo_2010 `
  --model Qwen/Qwen3.6-27B
```

验收标准：

- 一条命令跑完整流程。
- 每一步失败时停止。
- 最后打印 summary 路径和 HTML 路径。

## 15. 第 12 阶段：并发

等单线程稳定后，再加并发。

使用：

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
```

建议：

- 初始 `max_workers=2`。
- API 稳定后再到 4 或 8。
- 每题独立保存结果。
- 失败题保留错误文件。

不要一开始做复杂 fallback。失败就失败，先看清楚错误。

验收标准：

- 23 题能并发跑。
- 成功题和失败题都有记录。
- summary 有 success/error 数。

## 16. 第 13 阶段：限流和重试

先做简单版：

- 每次请求前 `sleep`。
- 429 或 timeout 时重试 2 次。
- 重试仍失败就记录 error。

再做进阶版：

- 估算输入 token。
- 每分钟维护一个 token bucket。
- 多进程共享 SQLite pool。

初学者阶段不必实现 SQLite TPM 池，但要理解为什么需要它：并发调用 LLM 时，如果不控 TPM，服务商会限流。

## 17. 第 14 阶段：PDF 和 OCR

等文本版稳定后，再接 PDF。

最简单路线：

1. 用 `pypdf` 拆页。
2. 用外部 OCR 服务或命令生成每页文本。
3. 保存成统一格式：

```json
[
  {
    "page": 1,
    "text": "...",
    "blocks": []
  }
]
```

不要一开始复刻复杂 OCR block、bbox、版面检测。先把 PDF 到文本跑通。

## 18. 项目里最重要的工程习惯

### 18.1 所有步骤都有产物

不要只在内存里传数据。每一步都写文件：

```text
step2/questions.json
question_bank/question_bank.json
audit/summary.json
rendered/index.html
```

这样出错时你能定位是哪一步坏了。

### 18.2 不要手改最终 JSON

如果 Q5 的 LaTeX 有问题，不要直接改 `q05.json`。应该：

- 改 prompt
- 改 schema
- 改审计规则
- 改后处理规则
- 然后重跑

### 18.3 失败要早暴露

不要写这种代码：

```python
try:
    ...
except Exception:
    return {}
```

应该保存错误并让 summary 体现失败。

### 18.4 Prompt 要统一语言

中文试卷的 prompt 全部用中文。不要中英混杂。

### 18.5 每次实验要留下证据

至少记录：

- 模型名
- provider
- 是否 tool calling
- 是否 image-only
- 耗时
- 成功数
- 错误数
- 输出路径

## 19. 推荐里程碑

### Milestone 1：纯规则文本版

输入 OCR 文本，输出 HTML。

完成条件：

- 5 道样例题可跑通。
- 不调用 LLM。
- 有测试。

### Milestone 2：LLM JSON 版

用普通 JSON 输出生成题库。

完成条件：

- 5 道题成功。
- 错误响应能保存。
- Pydantic 能拦住坏 JSON。

### Milestone 3：Tool Calling 版

用 tool calling 替换普通 JSON。

完成条件：

- 模型必须调用函数。
- arguments 校验通过。
- schema 错误能暴露。

### Milestone 4：审计版

加入 LaTeX 和内容审计。

完成条件：

- 能抓空题干。
- 能抓空答案。
- 能抓裸 `\frac`。

### Milestone 5：图片版

支持题面和答案图片。

完成条件：

- HTML 显示图片。
- LLM 接收图片。
- 支持 image-only 单题测试。

### Milestone 6：小型生产版

处理一整张 23 题试卷。

完成条件：

- 有 summary。
- 有 per-question 文件。
- 有 HTML 审核页。
- 有失败题列表。

## 20. 练习任务清单

基础任务：

- 写 `read_json` / `write_json`。
- 写 Pydantic model。
- 写题号切分。
- 写 summary。
- 写 HTML 模板。

中级任务：

- 写 LLM 请求。
- 写 tool calling。
- 写 per-question 输出。
- 写裸 LaTeX 审计。
- 写并发执行。

高级任务：

- 支持图片输入。
- 支持 PDF/OCR。
- 支持 TPM 限流。
- 支持 Step2 图片裁剪。
- 支持资产归属。

## 21. 你可以参考当前项目，但不要照抄

当前 skill 版项目已经比较复杂。作为学习者，建议只参考这些设计思想：

- pipeline 分步骤。
- 每一步有输入输出目录。
- JSON schema 明确。
- 模型输出必须校验。
- 渲染页用于人工审核。
- summary 记录真实证据。

不要一开始照抄：

- 多模型并发。
- SQLite TPM pool。
- 复杂视觉资产归属。
- 完整 PDF OCR 版面切分。
- 大量后处理规则。

先写出你自己能解释清楚的 500 行，再扩展到 2000 行。不要一开始写 1 万行。

## 22. 建议阅读顺序

如果你要对照当前 skill 版阅读，建议顺序：

1. `FUNCTIONAL_SPEC.zh-CN.md`
2. `assets/v11_runtime/step3_schema.py`
3. `assets/v11_runtime/step3_question2json.py`
4. `assets/v11_runtime/step5_vlm_html_mathjax_render.py`
5. `assets/v11_runtime/run_pipeline.py`
6. `assets/v11_runtime/step2_layout.py`
7. `assets/v11_runtime/step4_asset_distribution.py`

先读 schema 和 Step3，因为它们最能体现“题库 JSON 应该长什么样”。

## 23. 最小可行版本的验收样例

准备 `data/samples/tiny_ocr.txt`：

```text
1. 已知 $x+1=3$，求 $x$。
答案：$2$
解析：由 $x+1=3$，得 $x=2$。

2. 圆 C: x^2 + y^2 = 1 的半径为____。
答案：$1$
解析：标准圆方程半径为 $1$。
```

跑：

```powershell
python.exe -m mini_exam_bank.cli pipeline --input data\samples\tiny_ocr.txt --run-id tiny
```

期望产物：

```text
runs/tiny/
  step2/questions.json
  question_bank/question_bank.json
  question_bank/per_question/q01.json
  question_bank/per_question/q02.json
  audit/summary.json
  rendered/index.html
```

期望 summary：

```json
{
  "run_id": "tiny",
  "question_count": 2,
  "success_count": 2,
  "error_count": 0
}
```

## 24. 最后建议

你重做这个项目时，最重要的不是一次写对所有功能，而是保持每一步都可运行、可检查、可删除重来。

推荐工作方式：

- 每天只完成一个小目标。
- 每个目标都写一个样例输入。
- 每个目标都保存输出。
- 每次 bug 都先看真实产物。
- 只在理解问题后修改代码。

当你能独立实现“文本 OCR -> 切题 -> tool calling -> JSON 校验 -> HTML 渲染”这条链路时，再去挑战图片裁剪、PDF OCR 和资产归属，会容易很多。
