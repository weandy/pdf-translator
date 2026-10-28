"""由块内字符统计块的主样式（字号、粗细、衬线），供阅读器排版译文。"""

import statistics
from collections import Counter
from collections.abc import Sequence

from pdftr.domain.chars import Char
from pdftr.domain.ir import BlockStyle

_SANS = ("Sans", "Arial", "Helvetica", "Gothic", "Hei", "Grotesk", "Verdana", "Calibri", "Inter")


def is_serif(font: str) -> bool:
    return not any(k.lower() in font.lower() for k in _SANS)


def block_style(chars: Sequence[Char], fallback_size: float = 10.0) -> BlockStyle:
    visible = [c for c in chars if not c.c.isspace()]
    if not visible:
        return BlockStyle(font_size=fallback_size)
    n = len(visible)
    fonts = Counter(c.font for c in visible)
    colors = Counter(c.color for c in visible)
    return BlockStyle(
        font_size=round(statistics.median(c.size for c in visible), 2),
        bold=sum(c.bold for c in visible) > n / 2,
        italic=sum(c.italic for c in visible) > n / 2,
        serif=is_serif(fonts.most_common(1)[0][0]),
        color=colors.most_common(1)[0][0],
    )
