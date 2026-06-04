from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from exam_import.schemas.common import ValidationError


@dataclass(frozen=True)
class PacketGeometry:
    label: str
    page: int
    stream: str
    image_path: str
    bbox: tuple[float, float, float, float]
    reading_order: int

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PacketGeometry":
        label = str(payload.get("label") or "").strip()
        if not label:
            raise ValidationError("geometry label must not be empty")
        bbox_value = payload.get("bbox")
        if not isinstance(bbox_value, list) or len(bbox_value) < 4:
            raise ValidationError(f"geometry bbox is invalid for label {label}")
        return cls(
            label=label,
            page=int(payload.get("page") or 0),
            stream=str(payload.get("stream") or "").strip() or "question",
            image_path=str(payload.get("image_path") or "").strip(),
            bbox=(
                float(bbox_value[0]),
                float(bbox_value[1]),
                float(bbox_value[2]),
                float(bbox_value[3]),
            ),
            reading_order=int(payload.get("reading_order") or payload.get("order") or 0),
        )


@dataclass(frozen=True)
class CropIsland:
    page: int
    stream: str
    image_path: str
    labels: list[str]
    items: list[PacketGeometry]
    bbox: tuple[float, float, float, float]
    order: int


def build_crop_plan(
    labels: list[str],
    geometries: list[PacketGeometry],
    page_width_by_image: dict[str, float],
    margin: float = 8.0,
) -> list[CropIsland]:
    geometry_map = {item.label: item for item in geometries}
    selected = [geometry_map[label] for label in labels if label in geometry_map]
    if not selected:
        return []
    grouped: dict[tuple[str, int, str], list[PacketGeometry]] = {}
    for item in selected:
        grouped.setdefault((item.stream, item.page, item.image_path), []).append(item)
    islands: list[CropIsland] = []
    for (stream, page, image_path), items in grouped.items():
        page_width = page_width_by_image.get(image_path)
        if page_width is None:
            raise ValidationError(f"Missing page width for image: {image_path}")
        islands.extend(crop_islands_for_question_page(items, page_width=page_width, margin=margin))
    return sorted(islands, key=lambda item: (item.page, item.order, item.image_path))


def crop_islands_for_question_page(
    items: list[PacketGeometry],
    *,
    page_width: float,
    margin: float = 8.0,
) -> list[CropIsland]:
    if not items:
        return []
    inflated = [inflate_bbox(item.bbox, margin=margin) for item in items]
    graph: dict[int, set[int]] = {index: set() for index in range(len(items))}
    for i, box_a in enumerate(inflated):
        for j, box_b in enumerate(inflated):
            if i >= j:
                continue
            if should_merge(box_a, box_b, page_width):
                graph[i].add(j)
                graph[j].add(i)
    components = _connected_components(graph)
    crops: list[CropIsland] = []
    for component in components:
        component_items = [items[index] for index in component]
        component_boxes = [inflated[index] for index in component]
        crops.append(
            CropIsland(
                page=component_items[0].page,
                stream=component_items[0].stream,
                image_path=component_items[0].image_path,
                labels=[item.label for item in sorted(component_items, key=lambda entry: (entry.reading_order, entry.label))],
                items=sorted(component_items, key=lambda entry: (entry.reading_order, entry.label)),
                bbox=union_bbox(component_boxes),
                order=min(item.reading_order for item in component_items),
            )
        )
    return sorted(crops, key=lambda item: item.order)


def should_merge(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
    page_width: float,
) -> bool:
    x_overlap = overlap_len(box_a[0], box_a[2], box_b[0], box_b[2])
    min_width = min(box_a[2] - box_a[0], box_b[2] - box_b[0])
    y_gap = vertical_gap(box_a, box_b)
    x_center_gap = abs(((box_a[0] + box_a[2]) / 2.0) - ((box_b[0] + box_b[2]) / 2.0))
    same_column = x_overlap >= 0.25 * min_width
    close_vertically = y_gap <= 80.0
    near_same_text_flow = x_center_gap <= 0.35 * page_width
    return same_column and close_vertically and near_same_text_flow


def inflate_bbox(bbox: tuple[float, float, float, float], *, margin: float) -> tuple[float, float, float, float]:
    return (
        bbox[0] - margin,
        bbox[1] - margin,
        bbox[2] + margin,
        bbox[3] + margin,
    )


def union_bbox(boxes: Iterable[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    boxes = list(boxes)
    if not boxes:
        raise ValidationError("boxes must not be empty")
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def overlap_len(a1: float, a2: float, b1: float, b2: float) -> float:
    return max(0.0, min(a2, b2) - max(a1, b1))


def vertical_gap(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
) -> float:
    if box_a[3] < box_b[1]:
        return box_b[1] - box_a[3]
    if box_b[3] < box_a[1]:
        return box_a[1] - box_b[3]
    return 0.0


def _connected_components(graph: dict[int, set[int]]) -> list[list[int]]:
    remaining = set(graph)
    components: list[list[int]] = []
    while remaining:
        start = remaining.pop()
        stack = [start]
        component = [start]
        while stack:
            current = stack.pop()
            for neighbor in graph[current]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
                    component.append(neighbor)
        components.append(sorted(component))
    return components
