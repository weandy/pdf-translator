"""利用布局行和原生字形识别需要独立翻译的标题、日期和悬挂列表项。"""

import re
import statistics
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import pairwise

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType
from pdftr.domain.layout import LayoutBlock
from pdftr.domain.text import LETTER_HEADING, NUMBERED_HEADING, PUBLICATION_DATE, RICH_TAG, ROMAN_HEADING
from pdftr.fusion.formula_align import visual_lines
from pdftr.fusion.style import block_style
from pdftr.fusion.text_fix import main_metrics

_MARKER = re.compile(r"\([0-9]{1,6}\)|[0-9]{1,6}[.)]|[a-z][.)]|[•·▪◦‣]")
_PAGE_NUMBER = re.compile(r"[0-9]{1,6}")
_KEYWORDS_LINE = re.compile(r"^(?:key\s*words?|index\s+terms)\s*[.:：—–\-]", re.I)
_TRACK_TOL = 2.0


@dataclass
class MarginMark:
    text: str
    bbox: Rect
    chars: list[Char]
    size: float
    baseline: float | None

    @property
    def y(self) -> float:
        return self.baseline if self.baseline is not None else self.bbox.center[1]

    @property
    def series(self) -> tuple[str, int]:
        if self.text[0] in "•·▪◦‣":
            return "bullet", 0
        number = self.text.strip("().")
        return ("number", int(number)) if number.isdecimal() else ("letter", ord(number))


def _bounds(boxes: Sequence[Rect]) -> Rect:
    return Rect(
        x0=min(box.x0 for box in boxes), y0=min(box.y0 for box in boxes),
        x1=max(box.x1 for box in boxes), y1=max(box.y1 for box in boxes),
    )  # fmt: skip


def _short_groups(orphans: list[Char]) -> Iterator[list[Char]]:
    for row in visual_lines([c for c in orphans if c.upright]):
        group: list[Char] = []
        for ch in row:
            if group and (ch.c.isspace() or ch.bbox.x0 - group[-1].bbox.x1 > 0.5 * max(ch.size, group[-1].size)):
                yield group
                group = []
            if not ch.c.isspace():
                group.append(ch)
        if group:
            yield group


def _review_tracks(numbers: list[MarginMark], *, right: bool) -> list[float]:
    def edge(mark: MarginMark) -> float:
        return mark.bbox.x1 if right else mark.bbox.x0

    tracks: list[list[MarginMark]] = []
    for mark in sorted(numbers, key=edge):
        if not tracks or edge(mark) - edge(tracks[-1][0]) > _TRACK_TOL:
            tracks.append([])
        tracks[-1].append(mark)
    rejected: list[float] = []
    for track in tracks:
        track.sort(key=lambda mark: mark.y)
        for i in range(len(track) - 4):
            window = track[i : i + 5]
            gaps = [b.y - a.y for a, b in pairwise(window)]
            steps = {int(b.text) - int(a.text) for a, b in pairwise(window)}
            if (
                len(steps) == 1
                and min(steps) > 0
                and min(gaps) > 0
                and max(gaps) - min(gaps) <= max(0.5, 0.08 * statistics.median(gaps))
            ):
                rejected.append(statistics.median(edge(mark) for mark in window))
                break
    return rejected


def margin_marks(orphans: list[Char]) -> list[MarginMark]:
    marks: list[MarginMark] = []
    numbers: list[MarginMark] = []
    for group in _short_groups(orphans):
        text = "".join(c.c for c in group)
        if _MARKER.fullmatch(text) or _PAGE_NUMBER.fullmatch(text):
            size, baseline = main_metrics(group)
            mark = MarginMark(text, _bounds([c.bbox for c in group]), group, size, baseline)
            (numbers if _PAGE_NUMBER.fullmatch(text) else marks).append(mark)

    # 此类行号可右对齐，跨越位数时只比较左缘会漏掉整轨。
    left = _review_tracks(numbers, right=False)
    right = _review_tracks(numbers, right=True)
    return [
        mark for mark in marks
        if not any(abs(mark.bbox.x0 - x) <= _TRACK_TOL for x in left)
        and not any(abs(mark.bbox.x1 - x) <= _TRACK_TOL for x in right)
    ]  # fmt: skip


def _hanging_marks(block: LayoutBlock, rows: list[list[Char]], marks: list[MarginMark]) -> dict[int, MarginMark]:
    matches: dict[int, MarginMark] = {}
    for i, (line, chars) in enumerate(zip(block.lines, rows, strict=True)):
        if not chars:
            continue
        size, baseline = main_metrics(chars)
        center = statistics.median(c.bbox.center[1] for c in chars)
        margin = min(block.bbox.x0, line.bbox.x0)
        near = [
            mark
            for mark in marks
            if mark.bbox.x0 < margin
            and -0.5 * size <= margin - mark.bbox.x1 <= max(3.5 * size, 36.0)
            and (abs(baseline - mark.baseline) if baseline is not None and mark.baseline is not None
                 else abs(center - mark.bbox.center[1])) <= max(1.5, 0.25 * size)
        ]  # fmt: skip
        if near:
            matches[i] = max(near, key=lambda mark: mark.bbox.x1)
    if not matches:
        return matches
    starts = [mark.bbox.x0 for mark in matches.values()]
    if max(starts) - min(starts) > max(_TRACK_TOL, 0.5 * statistics.median(m.size for m in matches.values())):
        return {}
    for left, right in pairwise(matches.values()):
        kind, value = left.series
        next_kind, next_value = right.series
        if (
            kind != next_kind
            or (kind == "bullet" and left.text != right.text)
            or (kind != "bullet" and value >= next_value)
        ):
            return {}
    return matches


def _heading_cuts(block: LayoutBlock, texts: list[str], rows: list[list[Char]]) -> dict[int, int]:
    cuts: dict[int, int] = {}
    parent = texts[0]
    parent_line = 0
    parent_level = 1
    for i, text in enumerate(texts[1:], start=1):
        roman = ROMAN_HEADING.match(parent)
        chapter = NUMBERED_HEADING.match(parent)
        child = NUMBERED_HEADING.match(text)
        letter = LETTER_HEADING.match(text) and not ROMAN_HEADING.match(text)
        if roman and letter:
            level = 2
        elif roman and child and child[2]:
            level = 1 + child[2].count(".")
        elif (
            chapter and child and chapter[1].isdigit() and child[1].isdigit() and child[2].count(".") + 1 > parent_level
        ):
            level = child[2].count(".") + 1
        elif roman and child and child[1].isdigit() and _style_break(block, rows, i):
            level = 2
        else:
            continue
        cuts[parent_line] = parent_level
        cuts[i] = level
        parent, parent_line, parent_level = text, i, level
    return cuts


def _style_break(block: LayoutBlock, rows: list[list[Char]], index: int) -> bool:
    if not rows[index - 1] or not rows[index]:
        return False
    a, b = (block_style(chars, title=True) for chars in rows[index - 1 : index + 1])
    left, right = (line.bbox for line in block.lines[index - 1 : index + 1])
    return (
        abs(a.font_size - b.font_size) >= 0.8
        or a.italic != b.italic
        or a.bold != b.bold
        or (abs(left.x0 - right.x0) > a.font_size and abs(left.center[0] - right.center[0]) > a.font_size)
    )


def split_layout(
    block: LayoutBlock, chars: list[Char], marks: list[MarginMark], page_width: float
) -> tuple[list[tuple[LayoutBlock, list[Char], bool]], set[int]]:
    """返回按阅读顺序排列的子块，以及留在背景页的孤儿字形。"""
    if block.discarded or not block.lines or block.type not in {BlockType.TEXT, BlockType.TITLE}:
        return [(block, chars, False)], set()
    titles = block.type is BlockType.TITLE
    texts = [
        RICH_TAG.sub("", " ".join(s.content for s in line.spans if s.kind == "text")).strip() for line in block.lines
    ]
    dates = [i for i, text in enumerate(texts) if PUBLICATION_DATE.match(text)] if not titles else []
    if block.bbox.width >= page_width * 0.45 or len(dates) < 2:
        dates = []
    keywords = [i for i, text in enumerate(texts[1:], start=1) if _KEYWORDS_LINE.match(text)] if not titles else []
    if (titles and len(texts) == 1) or (not titles and not dates and not keywords and not marks):
        return [(block, chars, False)], set()

    rows: list[list[Char]] = [[] for _ in block.lines]
    centers = [line.bbox.center[1] for line in block.lines]
    for char in chars:
        i = min(range(len(centers)), key=lambda i: abs(char.bbox.center[1] - centers[i]))
        rows[i].append(char)
    levels = _heading_cuts(block, texts, rows) if titles else {}
    hanging = _hanging_marks(block, rows, marks) if not titles and not dates else {}
    if not (levels or dates or keywords or hanging):
        return [(block, chars, False)], set()
    boundaries = sorted({0, *levels, *dates, *keywords, *hanging, len(block.lines)})
    pieces: list[tuple[LayoutBlock, list[Char], bool]] = []
    for start, end in pairwise(boundaries):
        lines = block.lines[start:end]
        bbox = _bounds([line.bbox for line in lines]) if len(boundaries) > 2 else block.bbox
        if (mark := hanging.get(start)) and mark.bbox.x1 > bbox.x0:
            bbox = bbox.model_copy(update={"x0": mark.bbox.x1})
        piece = block.model_copy(update={"lines": lines, "bbox": bbox, "level": levels.get(start, block.level)})
        pieces.append((piece, [c for row in rows[start:end] for c in row], start in dates or start in hanging))
    return pieces, {id(char) for mark in hanging.values() for char in mark.chars}
