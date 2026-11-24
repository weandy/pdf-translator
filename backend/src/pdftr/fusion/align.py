"""把 PyMuPDF 字符按中心点归属到版面块（S4：5 篇论文覆盖率 ≥99.8%）。"""

from collections.abc import Sequence

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect

TOL = 1.5  # 块框外扩容差（pt）
_OVERFLOW_MAX_WIDTH = 32.0  # 行尾 overfull hbox 连续右溢出最大允许宽度（pt）
_OVERFLOW_ROW_TOL = 0.45  # 与行末字符同视觉行的竖直中心容差（字号倍数）
_OVERFLOW_GAP_MIN = -1.0  # 允许的最小字距（微重叠）
_OVERFLOW_GAP_MAX = 0.65  # 允许的最大字距（字号倍数）
_OVERFLOW_TAIL_PUNCT = frozenset(".,;:!?)]}’”")


def _touches_other_box(ch: Char, owner_idx: int, owner_box: Rect, boxes: Sequence[Rect]) -> bool:
    for j, other in enumerate(boxes):
        if j == owner_idx:
            continue
        if not (other.y0 - TOL < ch.bbox.y1 and ch.bbox.y0 < other.y1 + TOL and other.x1 > owner_box.x1 + TOL):
            continue
        # 行尾纯标点（如紧贴右栏前的句号）仅在真正侵入相邻块内部时才算触碰，避免残留孤立黑点。
        if ch.c in _OVERFLOW_TAIL_PUNCT:
            if ch.bbox.center[0] >= other.x0 or ch.bbox.x1 > other.x0 + 0.5:
                return True
        elif ch.bbox.x1 > other.x0 - TOL:
            return True
    return False


def _recover_right_overflow(assigned: list[list[Char]], orphans: list[Char], boxes: Sequence[Rect]) -> list[Char]:
    """回收紧接在块行尾右侧、不触碰其他块的短串右溢出字符（overfull hbox）。"""
    # 相邻右栏经 TOL 左扩后可能误吸左栏溢出词末尾的句末标点（中心仍在右栏左缘之外且右栏无同 line 正文）。
    borrowed_from: dict[int, int] = {}
    borrowed_chars: list[Char] = []
    for idx, (box, block_chars) in enumerate(zip(boxes, assigned, strict=True)):
        inner_lines = {ch.line for ch in block_chars if not ch.c.isspace() and ch.bbox.center[0] >= box.x0}
        for ch in block_chars:
            if (
                ch.upright
                and ch.c in _OVERFLOW_TAIL_PUNCT
                and ch.bbox.center[0] < box.x0
                and ch.line not in inner_lines
            ):
                borrowed_from[id(ch)] = idx
                borrowed_chars.append(ch)
    if not orphans and not borrowed_chars:
        return orphans
    anchors: list[tuple[float, int, Rect, Char]] = []
    for i, (box, block_chars) in enumerate(zip(boxes, assigned, strict=True)):
        by_line: dict[int, Char] = {}
        for ch in block_chars:
            if not ch.upright or ch.c.isspace() or id(ch) in borrowed_from:
                continue
            cur = by_line.get(ch.line)
            if cur is None or ch.bbox.x1 > cur.bbox.x1:
                by_line[ch.line] = ch
        for last in by_line.values():
            if last.bbox.x1 >= box.x1 - max(12.0, 1.5 * last.size):
                anchors.append((box.area, i, box, last))
    if not anchors:
        return orphans

    anchors.sort(key=lambda item: (item[0], -item[3].bbox.x1))
    available = sorted(
        (*(ch for ch in orphans if ch.upright), *borrowed_chars),
        key=lambda c: (c.bbox.x0, c.bbox.center[1]),
    )
    claimed: set[int] = set()
    reclaimed_from: dict[int, set[int]] = {}
    for _, owner_idx, box, last in anchors:
        prev_x1 = last.bbox.x1
        last_cy = last.bbox.center[1]
        max_gap = _OVERFLOW_GAP_MAX * last.size
        row_tol = _OVERFLOW_ROW_TOL * last.size
        for ch in available:
            cid = id(ch)
            if cid in claimed:
                continue
            src_idx = borrowed_from.get(cid)
            if src_idx is not None and (src_idx == owner_idx or ch.line != last.line):
                continue
            gap = ch.bbox.x0 - prev_x1
            if gap > max_gap:
                break
            if gap < _OVERFLOW_GAP_MIN or ch.bbox.x1 <= prev_x1 - 0.5:
                continue
            if abs(ch.bbox.center[1] - last_cy) > row_tol:
                continue
            if ch.line != last.line and not 0.75 * last.size <= ch.size <= 1.3 * last.size:
                continue
            if ch.bbox.x1 - box.x1 > _OVERFLOW_MAX_WIDTH:
                continue
            if _touches_other_box(ch, owner_idx, box, boxes):
                break
            assigned[owner_idx].append(ch)
            claimed.add(cid)
            if src_idx is not None:
                reclaimed_from.setdefault(src_idx, set()).add(cid)
            prev_x1 = max(prev_x1, ch.bbox.x1)
    if not claimed:
        return orphans
    for src_idx, removed in reclaimed_from.items():
        assigned[src_idx] = [ch for ch in assigned[src_idx] if id(ch) not in removed]
    return [ch for ch in orphans if id(ch) not in claimed]


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
    orphans = _recover_right_overflow(assigned, orphans, boxes)
    return assigned, orphans
