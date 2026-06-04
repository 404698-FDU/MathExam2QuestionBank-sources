from __future__ import annotations

from html import escape
import re

import markdown

from exam_import.schemas.question_record import OptionGroup, QuestionRecord

from .asset_resolver import AssetResolver


OPTION_TAG_RE = re.compile(r'<options no="([^"]+)">')
ASSET_TAG_RE = re.compile(r"<(img|table|chart) src=\"([^\"]+)\">")


def render_question_record(record: QuestionRecord, asset_resolver: AssetResolver) -> str:
    option_lookup = {group.no: group for group in record.options_markdown}
    stem_html = "".join(
        render_markdown_segment(segment, asset_resolver, option_lookup)
        for segment in record.stem_markdown
    )
    answer_html = "".join(render_markdown_segment(item, asset_resolver, {}) for item in record.answer_markdown)
    analysis_html = "".join(render_markdown_segment(item, asset_resolver, {}) for item in record.analysis_markdown)
    issues_html = "".join(
        f'<li><strong>{escape(issue.type)}</strong>: {escape(issue.message)}</li>'
        for issue in record.issues
    )
    return (
        f'<article class="question-card" id="q{record.question_no}">'
        f'<header class="question-header"><h2>第 {record.question_no} 题</h2></header>'
        f'<section class="question-section"><h3>题面</h3>{stem_html or "<p class=\"empty\">无</p>"}</section>'
        f'<section class="question-section"><h3>答案</h3>{answer_html or "<p class=\"empty\">无</p>"}</section>'
        f'<section class="question-section"><h3>解析</h3>{analysis_html or "<p class=\"empty\">无</p>"}</section>'
        f'<section class="question-section"><h3>问题标记</h3><ul>{issues_html or "<li>无</li>"}</ul></section>'
        "</article>"
    )


def render_markdown_segment(
    segment: str,
    asset_resolver: AssetResolver,
    option_lookup: dict[str, OptionGroup],
) -> str:
    substituted = segment
    substituted = substituted.replace("<blank>", '<span class="blank-slot"></span>')
    substituted = substituted.replace("<choice_blank>", '<span class="choice-slot"></span>')
    substituted = ASSET_TAG_RE.sub(lambda match: asset_resolver.render_asset_html(match.group(1), match.group(2)), substituted)
    substituted = OPTION_TAG_RE.sub(lambda match: render_options_group(option_lookup.get(match.group(1)), asset_resolver), substituted)
    html = markdown.markdown(
        substituted,
        extensions=["extra", "sane_lists"],
        output_format="html5",
    )
    return html


def render_options_group(group: OptionGroup | None, asset_resolver: AssetResolver) -> str:
    if group is None:
        return '<div class="options-missing">Missing options group</div>'
    items = []
    for option in group.options:
        body = "".join(render_markdown_segment(item, asset_resolver, {}) for item in option.content_markdown)
        items.append(
            f'<li class="option-item"><span class="option-label">{escape(option.label)}</span><div class="option-content">{body}</div></li>'
        )
    return f'<ol class="options-group" data-options-no="{escape(group.no)}">{"".join(items)}</ol>'
