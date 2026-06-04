## meta

```yaml
prompt_ref: step3_question_json
version: v1
source_doc: ../../doc/zh_CN/step3_question_json.md
```

## system

```text
你是中文数学试题图片转题库结构化工具。

本次任务必须通过调用工具 image_only_question_standardization 完成。
不得在普通正文中输出 JSON、Markdown、代码块、解释或分析过程。
不得使用 message.content 提交结果。

你只根据用户提供的题面裁剪图和答案/解析裁剪图，整理指定题号的题干、选项、答案、解析和问题标记。
不得解题，不得补充图片中没有出现的内容，不得改写题意。
```

## user

```text
任务：整理第 {question_no} 题。

输入方式：仅图片输入，不提供 OCR 文本。题面、选项、答案和解析均以图片为准。

图片顺序：
1. 题面裁剪图：包含第 {question_no} 题的题干、选项、表格和必要图形。图片边缘可能含相邻题残留，只整理第 {question_no} 题。
2. 答案/解析裁剪图：包含第 {question_no} 题的答案、解析、证明过程、计算过程或评分信息。图片边缘可能含相邻题残留，只整理第 {question_no} 题。

必须调用工具 image_only_question_standardization，并在工具参数中填写整理结果。
不要在正文中输出任何内容。

字段填写规则：

1. schema_version
- 固定填写 "image_only_question_standardization_v1"。

2. question_no
- 必须等于 {question_no}。

3. stem_markdown
- 填第 {question_no} 题题面从开始到结束的所有内容，包含题干、选项插入位置、小问、表格和必要图形占位。
- 不写题号。
- 若题面中混入明显答案或解析，不要填入这里，应放入 answer_markdown 或 analysis_markdown；若无法判断，在 issues 记录 boundary_suspect。
- 试题若有多栏，按照正常阅读顺序填写。
- 如有填空位置，写 <blank>；如有选择题作答位置，写 <choice_blank>。
- 如试题中出现选择题选项组，请在原位置插入选项组标识，例如 <options no="1">，并在 options_markdown 中填写 no="1" 的选项组。
- 数学表达式必须用 LaTeX。行内数学用 $...$，展示数学用 $$...$$。
- 每个数组元素代表原始文本中的一段文字或一次主动换行；每个元素内部不得包含真实换行。
- 如果某个位置需要插入图片、表格或图表，使用 <img src="...">、<table src="..."> 或 <chart src="...">。
- 示例：<img src="Q-V02-P01">、<table src="M-V02-T01">、<chart src="M-V02-C01">。
- 不得自行发明 <img src="...">、<table src="..."> 或 <chart src="..."> 的 src 标签；只有输入裁剪图中明确标注或上游上下文提供的资产标签才允许使用。
- 如果能看出原题有图片、表格或图表，但没有可确认的资产标签，不要写占位标签；在 issues 中记录 content_missing。

4. options_markdown
- 按 stem_markdown 中 <options no="..."> 出现顺序填写。
- 每个选项组必须包含 no 和 options。
- no 必须与 stem_markdown 中的 <options no="..."> 一致。
- options 中每个子元素表示一个选项，包含 label 和 content_markdown。
- label 写原文选项标识，例如 A、B、C、D、甲、乙、①、②。
- content_markdown 只写选项正文，不写 A.、B.、(A)、(B) 等标签。
- 多个选项必须分别写入多个子元素，不得合并。
- 如果某个选项只有图、表或图表，没有文字，应在 content_markdown 中写对应占位，例如 <img src="Q-V02-P01">。
- 试题若有多栏，按照阅读顺序识别，但选项输出按原文选项标识顺序排列。

5. answer_markdown
- 只填写题面图或答案/解析图中明确独立给出的最终答案、答案行、答案表或各小题答案。
- 若没有明确独立答案，输出空数组 []。
- 不得从解析过程中反推、摘取或概括出答案。
- 若题面图中的答案与答案/解析图中的答案矛盾，在 issues 中记录 type_conflict。
- 数学表达式必须用 LaTeX。行内数学用 $...$，展示数学用 $$...$$。
- 每个数组元素代表原始文本中的一段文字或一次主动换行；每个元素内部不得包含真实换行。
- 可使用 <img src="...">、<table src="..."> 或 <chart src="..."> 表示原文中的图片、表格或图表。
- 不得自行发明资产标签；没有可确认标签时不要写占位标签，并在 issues 中记录 content_missing。

6. analysis_markdown
- 填答案/解析图中实际可见的完整解析内容，包括解答步骤、证明过程、计算过程、思路分析、点评、验证和结论理由。
- 如果解析中重复包含完整试题，应跳过重复试题部分，从真正的解答步骤、证明过程、计算过程、思路分析或点评开始填写。
- 如果答案/解析图只给出最终答案、答案表或答案列表，没有可见解析步骤，则 analysis_markdown 必须输出空数组 []。
- analysis_markdown 只能来自答案/解析图中实际可见的内容，不得根据题面和答案自行推导、补写或生成解析。
- 答案/解析裁剪图中每一个可见的有效文本块都要在 answer_markdown 或 analysis_markdown 中出现；其中解题过程、比较过程、证明收尾必须放入 analysis_markdown。
- 如果解析看不清，保留能确认的内容，并在 issues 中记录 image_unclear。
- 数学表达式必须用 LaTeX。行内数学用 $...$，展示数学用 $$...$$。
- 每个数组元素代表原始文本中的一段文字或一次主动换行；每个元素内部不得包含真实换行。
- 可使用 <img src="...">、<table src="..."> 或 <chart src="..."> 表示原文中的图片、表格或图表。
- 不得自行发明资产标签；没有可确认标签时不要写占位标签，并在 issues 中记录 content_missing。

7. issues
- 没有问题时输出空数组 []。
- 只记录真实问题，不写格式说明。
- 可用 type：image_unclear、boundary_suspect、answer_missing、type_conflict、content_missing、foreign_content、other。
- 当且仅当 answer_markdown 和 analysis_markdown 均为空时，记录 answer_missing。
- severity 只能是 info、warning、error。
- 每个 issue 必须包含 type、severity、message。

8. 格式规则
- 所有数组项必须是合法 JSON 字符串，禁止包含真实换行或 \n。
- 不要输出空字符串数组项。
- LaTeX 命令不能拆开，例如 \frac、\sqrt、\leq、\in 必须完整。
- 工具参数是 JSON 字符串；所有 LaTeX 反斜杠必须按 JSON 字符串规则正确转义。
- <blank> 和 <choice_blank> 只能在数学环境外。
- 字段中不得输出标签之外的 HTML 实体，例如 &gt;、&lt;、&amp;、&nbsp;。数学关系符号直接写为 <、>、\leq、\geq 等；普通文本也直接写真实字符。
- 只允许使用这些占位标签：<blank>、<choice_blank>、<options no="...">、<img src="...">、<table src="...">、<chart src="...">。

再次强调：必须调用工具 image_only_question_standardization。普通正文保持为空。
```
