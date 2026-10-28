"""把 PyMuPDF 字符按中心点归属到版面块（S4：5 篇论文覆盖率 ≥99.8%）。"""

from collections.abc import Sequence

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect

TOL = 1.5  # 块框外扩容差（pt）


def align(chars: Sequence[Char], boxes: Sequence[Rect]) -> tuple[list[list[Char]], list[Char]]:
    """返回每个块分到的字符（与 boxes 同序），以及没有归属的字符。

    一个字符同时落在多个块里时，归给面积最小的块（嵌套时更具体的那个）。
    """
    assigned: list[list[Char]] = [[] for _ in boxes]
    orphans: list[Char] = []
    for ch in chars:
        x, y = ch.bbox.center
        best: int | None = None
        best_area = float("inf")
        for i, box in enumerate(boxes):
            if box.contains_point(x, y, TOL) and box.area < best_area:
                best, best_area = i, box.area
        if best is None:
            orphans.append(ch)
        else:
            assigned[best].append(ch)
    return assigned, orphans
