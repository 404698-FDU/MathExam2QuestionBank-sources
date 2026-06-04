# Step2 裁剪图算法设计

来源：Step2 范围检测后处理与 Step3 输入裁剪图设计。

目标：根据 Step2 输出的题号范围 label 生成 Step3 使用的题面裁剪图和答案/解析裁剪图。Step2 模型只输出 label 范围；裁剪图由本地代码根据 label、bbox 和页面图生成。

## 基本职责

Step2 裁剪阶段读取：

```text
qa_alignment.json
question_packets/page_*.json
answer_packets/page_*.json
mixed_packets/page_*.json
source_runs/<run_id>/{paper,answer,mixed}/pages/page_*.png
```

裁剪阶段不调用大模型，不修改 Step2 范围，不重新判断题号。

## 当前基础规则

### 1. 以 label 聚合 bbox

每道题从 Step2 后处理产物读取 label：

```text
qa_alignment[].question.labels
qa_alignment[].question.surface_labels
qa_alignment[].answer.items[].labels
```

每个 label 在 packet blocks 中查找：

```text
page
stream
bbox
page_image
annotated_page_image
```

### 2. 同页同题分组

同一道题的 labels 按：

```text
stream + page + image_path
```

分组。跨页题目不会拼成长图，而是每页生成一张或多张裁剪图。

### 3. bbox 坐标缩放

Step2 block bbox 使用归一化坐标时，应缩放到真实图片尺寸：

```text
x_image = x / coord_width * image_width
y_image = y / coord_height * image_height
```

默认 `coord_width=1000`、`coord_height=1000`。

### 4. 共享 bbox 拆分

如果 OCR 把多行文本给到同一个 bbox，且 block_id 中能识别 `_l1`、`_l2` 这类行号，本地应按文本显示宽度估算每行高度，把共享 bbox 拆成更细的有效 bbox，避免裁剪过大。

### 5. visual_labels 参与裁剪

如果 Step2 把范围外但属于该题的图片、表格、图表放入 `visual_labels`，这些 label 应参与该题裁剪。但 Step4 仍负责最终资产占位对账。

### 6. Step3 裁剪图只标注资产框

Step3 使用的单题裁剪图与 Step2 范围识别用的整页标注图不同：

```text
Step2 整页标注图：保留文本块 label 和资产 label，用于模型判断题号范围。
Step3 单题裁剪图：只标注资产框 label，不标注文本块 label。
```

Step3 裁剪图写入时应在裁剪后的图片内叠加资产框：

```text
只绘制 P/T/C 类视觉资产 label。
label 放在资产框内部左上角。
不得在框外绘制 label。
不得绘制 B 类文本块 label。
```

这样 Step3 模型能够读取真实资产 label 并生成 `<img src="...">`、`<table src="...">` 或 `<chart src="...">` 占位，同时不会被大量文本块 label 干扰题面 OCR。

## 同页跨栏裁剪问题

原先同页同题裁剪方式是：

```text
同一题同一页所有 bbox -> union bbox -> 裁一张图
```

当一道题同页跨栏时，左栏 bbox 和右栏 bbox 的 union 会把两栏中间无关内容一起裁进去。

示例：

```text
左栏题干 bbox + 右栏续写 bbox -> 一个超宽 bbox
```

这会污染 Step3 输入图。

## crop island 算法

为解决同页跨栏时 union bbox 过大的问题，同页同题不再直接整体 union，而是先拆成多个空间连通的裁剪岛。

处理流程：

```text
同一道题同一页 labels
  -> 取所有 bbox
  -> bbox 加 margin
  -> 按空间连通性聚类为 crop islands
  -> 每个 island 单独 union
  -> 每个 island 输出一张裁剪图
  -> 按 island 内最小 reading_order 排序
```

### 合并判断

两个 bbox 只有在明显属于同一个局部阅读区域时才合并。

建议条件：

```text
same_column = 横向重叠 >= min(width_a, width_b) * 0.25
close_vertically = 垂直间距 <= 80px
near_same_text_flow = x 中心距离 <= page_width * 0.35
```

合并规则：

```text
should_merge = same_column and close_vertically and near_same_text_flow
```

如果两个 bbox 在同页但横向间隔很大，且 x 区间几乎不重叠，不得合并。

### 伪代码

```python
def crop_islands_for_question_page(items, page_width):
    boxes = [inflate(item.bbox, margin=8) for item in items]
    graph = Graph(len(boxes))

    for i, a in enumerate(boxes):
        for j, b in enumerate(boxes):
            if i >= j:
                continue
            if should_merge(a, b, page_width):
                graph.add_edge(i, j)

    crops = []
    for island in graph.connected_components():
        crop_bbox = union([boxes[i] for i in island])
        order = min(items[i].reading_order for i in island)
        crops.append({"bbox": crop_bbox, "order": order})

    return sorted(crops, key=lambda item: item["order"])


def should_merge(a, b, page_width):
    x_overlap = overlap_len(a.x1, a.x2, b.x1, b.x2)
    min_width = min(a.width, b.width)
    y_gap = vertical_gap(a, b)
    x_center_gap = abs(a.cx - b.cx)

    same_column = x_overlap >= 0.25 * min_width
    close_vertically = y_gap <= 80
    near_same_text_flow = x_center_gap <= 0.35 * page_width

    return same_column and close_vertically and near_same_text_flow
```

## 输出规则

### 非跨栏

如果同页同题只有一个 crop island：

```text
<run_id>_q12_question_p001_1.png
```

### 同页跨栏

如果同页同题被拆成多个 crop island：

```text
<run_id>_q12_question_p001_1.png
<run_id>_q12_question_p001_2.png
```

这些图不是并列候选，而是同一道题的连续阅读片段。

### 图片顺序

Step3 输入图片顺序必须等于阅读顺序：

```text
page 升序
crop island order 升序
```

Step3 prompt 应明确：

```text
同一道题可能有多张题面裁剪图；图片顺序即阅读顺序。
```

## 不改变的边界

crop island 算法只解决：

```text
同页跨栏时 union bbox 吃进中间无关区域
```

它不改变：

- Step2 的 `start_label/end_label`。
- Step2 的 `visual_labels`。
- Step4 的资产占位对账职责。
- Step3 的字段结构。
- 题号范围检测逻辑。

## 失败和审计

裁剪阶段应记录以下风险：

```text
missing_label_bbox：label 找不到 bbox。
missing_page_image：页面图不存在。
empty_crop_bbox：union 或 island bbox 无效。
multi_island_crop：同页同题被拆成多个 crop island。
oversized_crop：单张 crop 面积异常大，可能仍包含无关区域。
```

其中 `multi_island_crop` 是信息项，不是错误。
