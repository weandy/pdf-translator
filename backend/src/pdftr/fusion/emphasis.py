"""把可靠的 PDF 字符字重映射到解析器选定的正文，保留原文字面内容。"""

import unicodedata
from collections.abc import Sequence

from pdftr.domain.chars import Char


def _visible(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text) if not c.isspace())


def bold_ranges(text: str, chars: Sequence[Char]) -> tuple[bool, list[tuple[int, int]]]:
    """只有归一化正文完全对应才转移粗体；OCR 差异、漏字时宁可不猜强调位置。

    连字（如 ﬁ）会展开成多个字符，范围仍使用原始解析文字的索引；空白只在两侧都粗体时归入。
    不拆 Span，避免同一单词因字体切换被 segmenter 插入空格。
    """
    ordered = sorted(chars, key=lambda c: (c.line, c.bbox.x0))
    pdf = "".join(_visible(c.c) for c in ordered)
    wanted = "".join(_visible(c) for c in text)
    if not wanted or pdf != wanted:
        return False, []
    weights = [c.bold for c in ordered for _ in _visible(c.c)]
    mask = [False] * len(text)
    offset = 0
    for i, c in enumerate(text):
        n = len(_visible(c))
        if n:
            mask[i] = all(weights[offset : offset + n])
            offset += n
    # 含连续空白时也把整个空白段纳入同一强调范围，避免每个单词都开闭一次标签。
    i = 0
    while i < len(text):
        if not text[i].isspace():
            i += 1
            continue
        end = i + 1
        while end < len(text) and text[end].isspace():
            end += 1
        if i > 0 and end < len(text) and mask[i - 1] and mask[end]:
            mask[i:end] = [True] * (end - i)
        i = end
    visible = [value for c, value in zip(text, mask, strict=True) if not c.isspace()]
    if all(visible):
        return True, []
    ranges: list[tuple[int, int]] = []
    start = None
    for i, value in enumerate([*mask, False]):
        if value and start is None:
            start = i
        elif not value and start is not None:
            ranges.append((start, i))
            start = None
    return False, ranges
