"""由块内字符统计块的主样式（字号、粗细、衬线），供阅读器排版译文。"""

import statistics
from collections import Counter
from collections.abc import Sequence

from pdftr.domain.chars import Char
from pdftr.domain.ir import BlockStyle

_SANS = ("Sans", "Arial", "Helvetica", "Gothic", "Hei", "Grotesk", "Verdana", "Calibri", "Inter")


def is_serif(font: str) -> bool:
    return not any(k.lower() in font.lower() for k in _SANS)


def block_style(chars: Sequence[Char], fallback_size: float = 10.0, *, title: bool = False) -> BlockStyle:
    visible = [c for c in chars if not c.c.isspace()]
    if not visible:
        return BlockStyle(font_size=fallback_size)
    fonts = Counter(c.font for c in visible)
    colors = Counter(c.color for c in visible)
    sizes = [c.size for c in visible]
    median = statistics.median(sizes)
    if title:
        # small-caps 章节标题常把首字母排得更大，其余字母排成小型大写。
        # 取接近中位数的最大字号，既保留首字母层级，也避免脚注/异常字形的离群值抬高整段。
        candidates = [size for size in sizes if size <= median * 1.35]
        font_size = max(candidates or [median])
    else:
        font_size = median
    return BlockStyle(
        font_size=round(font_size, 2),
        # 混合正文不能按多数票整块加粗，否则少数常规字重丢失；局部强调由 Span 范围保留。
        bold=all(c.bold for c in visible),
        italic=all(c.italic for c in visible),
        serif=is_serif(fonts.most_common(1)[0][0]),
        color=colors.most_common(1)[0][0],
    )
