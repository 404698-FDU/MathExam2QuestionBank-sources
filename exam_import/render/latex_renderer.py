from __future__ import annotations

from html import escape
import re
from typing import Any, Mapping

from exam_import.schemas.question_record import OptionGroup, QuestionRecord

from .asset_resolver import AssetResolver


OPTION_TAG_RE = re.compile(r'<options no="([^"]+)"\s*/?>')
ASSET_TAG_RE = re.compile(r'<(img|table|chart)\s+src="([^"]+)">')
RAW_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
CONTROLLED_TAG_RE = re.compile(r'<options no="[^"]+"\s*/?>|<(?:img|table|chart)\s+src="[^"]+">')


def render_question_record(
    record: QuestionRecord,
    asset_resolver: AssetResolver,
    step2_crops: Mapping[str, list[str]] | None = None,
) -> str:
    option_lookup = {group.no: group for group in record.options_latex}
    stem_html = "".join(render_latex_block(block, asset_resolver, option_lookup) for block in record.stem_latex)
    answer_html = "".join(render_latex_block(block, asset_resolver, {}) for block in record.answer_latex)
    analysis_html = "".join(render_latex_block(block, asset_resolver, {}) for block in record.analysis_latex)
    issues_html = "".join(
        f"<li><b>{escape(issue.severity)}</b> {escape(issue.type)}: {escape(issue.message)}</li>"
        for issue in record.issues
    )
    return (
        f'<section class="question" id="q{record.question_no:03d}">'
        f"<h2>Q{record.question_no}</h2>"
        f"{render_step2_crop_section(record.question_no, step2_crops)}"
        f'<div class="stem">{stem_html or empty_block()}</div>'
        f"{render_content_section('答案', answer_html, 'answer-section')}"
        f"{render_content_section('解析', analysis_html, 'analysis-section', collapsible=True)}"
        f"{render_issue_section(issues_html)}"
        "</section>"
    )


def render_latex_options(groups: list[OptionGroup], asset_resolver: AssetResolver) -> str:
    rendered_groups = [render_options_group(group, asset_resolver) for group in groups]
    rendered_groups = [item for item in rendered_groups if item]
    if not rendered_groups:
        return ""
    return "".join(rendered_groups)


def render_options_group(group: OptionGroup, asset_resolver: AssetResolver) -> str:
    items = []
    for option in group.options:
        blocks = "".join(render_latex_block(block, asset_resolver, {}) for block in option.content_latex)
        if not blocks:
            blocks = empty_block()
        items.append(
            '<div class="option">'
            f'<div class="option-key">{escape(option.label)}.</div>'
            f'<div class="option-body">{blocks}</div>'
            "</div>"
        )
    if not items:
        return ""
    return f'<div class="options" data-options-no="{escape(group.no)}">{"".join(items)}</div>'


def render_content_section(title: str, body_html: str, class_name: str, collapsible: bool = False) -> str:
    if not body_html:
        return ""
    if collapsible:
        return (
            f'<details class="qa-section {class_name}">'
            f"<summary>{escape(title)}</summary>"
            f'<div class="qa-body">{body_html}</div>'
            "</details>"
        )
    return (
        f'<div class="qa-section {class_name}">'
        f"<h3>{escape(title)}</h3>"
        f'<div class="qa-body">{body_html}</div>'
        "</div>"
    )


def render_issue_section(issues_html: str) -> str:
    if not issues_html:
        return ""
    return f'<details class="issues"><summary>Issues</summary><ul>{issues_html}</ul></details>'


def render_step2_crop_section(qno: int, step2_crops: Mapping[str, list[str]] | None) -> str:
    question_crops = _normalize_crop_paths((step2_crops or {}).get("question"))
    answer_crops = _normalize_crop_paths((step2_crops or {}).get("answer"))
    if not question_crops and not answer_crops:
        return ""
    return (
        '<details class="step2-crops">'
        "<summary>Step2 裁剪</summary>"
        f"{render_step2_crop_group('题面', qno, question_crops)}"
        f"{render_step2_crop_group('答案/解析', qno, answer_crops)}"
        "</details>"
    )


def render_step2_crop_group(title: str, qno: int, crop_paths: list[str]) -> str:
    if not crop_paths:
        return ""
    figures = "".join(
        '<figure class="step2-crop-card">'
        f'<img src="{escape(path)}" alt="{escape(title)} Q{qno}">'
        "</figure>"
        for path in crop_paths
    )
    return f'<div class="step2-crop-group"><h3>{escape(title)}</h3><div class="step2-crop-grid">{figures}</div></div>'


def _normalize_crop_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def render_latex_block(
    block: str,
    asset_resolver: AssetResolver,
    option_lookup: dict[str, OptionGroup],
) -> str:
    text = str(block or "").strip()
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for start, end, value in iter_table_matches(text):
        before = text[last:start]
        if before.strip():
            parts.extend(render_controlled_text(before, asset_resolver, option_lookup))
        parts.append(f'<div class="table-wrap">{value}</div>')
        last = end
    tail = text[last:]
    if tail.strip():
        parts.extend(render_controlled_text(tail, asset_resolver, option_lookup))
    return "".join(parts)


def render_controlled_text(
    text: str,
    asset_resolver: AssetResolver,
    option_lookup: dict[str, OptionGroup],
) -> list[str]:
    parts: list[str] = []
    cursor = 0
    for match in CONTROLLED_TAG_RE.finditer(text):
        before = text[cursor:match.start()]
        if before.strip():
            parts.append(f'<p class="text-block">{render_inline_text(before)}</p>')
        token = match.group(0)
        option_match = OPTION_TAG_RE.fullmatch(token)
        if option_match:
            group = option_lookup.get(option_match.group(1))
            if group is None:
                parts.append('<div class="options-missing">Missing options group</div>')
            else:
                parts.append(render_options_group(group, asset_resolver))
        else:
            asset_match = ASSET_TAG_RE.fullmatch(token)
            if asset_match:
                parts.append(asset_resolver.render_asset_html(asset_match.group(1), asset_match.group(2)))
        cursor = match.end()
    tail = text[cursor:]
    if tail.strip():
        parts.append(f'<p class="text-block">{render_inline_text(tail)}</p>')
    return parts


def render_inline_text(text: str) -> str:
    return "".join(
        render_math_segment(segment) if is_math else render_text_segment(segment)
        for is_math, segment in split_math_segments(text)
    )


def render_text_segment(text: str) -> str:
    parts: list[str] = []
    last = 0
    for match in re.finditer(r"<(?:blank|choice_blank)>", text):
        before = text[last:match.start()]
        parts.append(render_plain_text_with_blanks(before))
        if match.group(0) == "<choice_blank>":
            parts.append('<span class="choice-blank"><span class="choice-blank-space"></span></span>')
        else:
            parts.append('<span class="blank"></span>')
        last = match.end()
    parts.append(render_plain_text_with_blanks(text[last:]))
    return "".join(parts).replace("\n", "<br>")


def render_plain_text_with_blanks(text: str) -> str:
    value = escape(text)
    value = re.sub(
        r"[\(（]\s*_{2,}\s*[\)）]",
        '<span class="choice-blank"><span class="choice-blank-space"></span></span>',
        value,
    )
    return re.sub(r"_{2,}", '<span class="blank"></span>', value)


def render_math_segment(text: str) -> str:
    blank_tex = r"\underline{\hspace{3em}}"
    choice_blank_tex = r"(\hspace{1.8em})"
    value = text.replace(r"\blankline{}", blank_tex)
    value = value.replace("<choice_blank>", choice_blank_tex).replace("<blank>", blank_tex)
    value = re.sub(r"_{2,}", lambda _: blank_tex, value)
    return escape(value)


def split_math_segments(text: str) -> list[tuple[bool, str]]:
    segments: list[tuple[bool, str]] = []
    start = 0
    cursor = 0
    while cursor < len(text):
        delimiter: tuple[str, str] | None = None
        if text.startswith(r"\(", cursor):
            delimiter = (r"\(", r"\)")
        elif text.startswith(r"\[", cursor):
            delimiter = (r"\[", r"\]")
        elif text.startswith("$$", cursor) and not is_escaped(text, cursor):
            delimiter = ("$$", "$$")
        elif text[cursor] == "$" and not is_escaped(text, cursor):
            delimiter = ("$", "$")
        if delimiter is None:
            cursor += 1
            continue
        opener, closer = delimiter
        end = find_unescaped(text, closer, cursor + len(opener))
        if end < 0:
            cursor += len(opener)
            continue
        if cursor > start:
            segments.append((False, text[start:cursor]))
        segment_end = end + len(closer)
        segments.append((True, text[cursor:segment_end]))
        cursor = segment_end
        start = cursor
    if start < len(text):
        segments.append((False, text[start:]))
    return segments


def find_unescaped(text: str, token: str, start: int) -> int:
    cursor = start
    while True:
        index = text.find(token, cursor)
        if index < 0:
            return -1
        if not is_escaped(text, index):
            return index
        cursor = index + len(token)


def is_escaped(text: str, index: int) -> bool:
    count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        count += 1
        cursor -= 1
    return count % 2 == 1


def iter_table_matches(text: str) -> list[tuple[int, int, str]]:
    return [(match.start(), match.end(), match.group(0)) for match in RAW_HTML_TABLE_RE.finditer(text)]


def empty_block() -> str:
    return '<p class="empty">(empty)</p>'
