from __future__ import annotations


def is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def find_unescaped(text: str, token: str, start: int) -> int:
    cursor = start
    while True:
        index = text.find(token, cursor)
        if index < 0:
            return -1
        if not is_escaped(text, index):
            return index
        cursor = index + len(token)


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
        if not delimiter:
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
