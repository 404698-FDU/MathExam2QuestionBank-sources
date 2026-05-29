# MathExam2QuestionBank Sources

这是上海数学试卷题库构建流程的脚本与导入素材目录。代码主要覆盖从 PDF/微信公众号素材导入、页面布局解析、题目 JSON 生成、JSON 审计修正、资产分发，到 MathJax HTML 渲染的流水线。

## 目录内容

- `run_pipeline.py`：单次题库构建流水线入口。
- `batch_import_2017_2026.py`：批量导入 2017-2026 年试卷素材的入口。
- `run_answer_patch.py`：答案补丁流程入口。
- `step2_layout.py`：试卷页面布局与题块识别。
- `step3_question2json.py`：题目结构化 JSON 生成。
- `step3_5_question_json_audit_fix.py`：题目 JSON 审计与修正。
- `step4_asset_distribution.py`：题目资产分发与同步。
- `step5_vlm_html_mathjax_render.py`：题库 MathJax HTML 渲染。
- `common_*.py`：共享 IO、LLM、数学文本、token 预算和裁图工具。
- `step0_wechat_import/`：微信公众号素材导入脚本及当前导入素材。

## 环境依赖

脚本需要 Python 3.10+。直接依赖包括：

- `pydantic`
- `pypdf`
- `Pillow`
- `requests`

部分脚本还依赖本项目上层目录中的模块，例如 `exam_agent_pipeline.py` 和 `question_bank_app.py`。当前路径约定来自原工作区布局：

```text
Research/
  gaokaomath_shanghai/
  gaokao_papers/
  MathExam2QuestionBank/
    code/
      sources/
```

如果将本仓库单独克隆到其他位置，需要同步这些相邻目录，或调整脚本中的路径常量。

## 密钥配置

LLM 调用通过环境变量或上层 token 文件读取密钥。不要把密钥提交到 Git。

可用环境变量：

```powershell
$env:LLM_API_KEY="..."
$env:SILICONFLOW_API_KEY="..."
$env:DASHSCOPE_API_KEY="..."
$env:BAILIAN_API_KEY="..."
```

脚本也会读取上层目录中的 `.siliconflow_token`、`.silliconflow_token`、`.bailian_token` 和 `.mineru_token`。这些文件已在 `.gitignore` 中排除。

## 常用命令

查看主流水线参数：

```powershell
python.exe -u run_pipeline.py --help
```

查看批量导入参数：

```powershell
python.exe -u batch_import_2017_2026.py --help
```

查看答案补丁参数：

```powershell
python.exe -u run_answer_patch.py --help
```

建议在 PowerShell 中运行中文输出脚本前设置 UTF-8：

```powershell
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

## 运行输出

流水线默认会在本目录下生成运行目录，例如：

- `runs/`
- `runs_step2_exam_blocks/`
- `runs_question_bank/`
- `reviews_step3_5_latex_audit/`
- `rendered_question_bank_mathjax/`

这些目录属于运行产物，默认不提交到仓库。需要保留结果时，应在任务结束后单独同步或归档。
