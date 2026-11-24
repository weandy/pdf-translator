"""跨栏/跨页段落的续行归位（S4）。

MinerU 把同一段落的后续行并进前一块（这些行的坐标仍是它们实际所在的位置），
原位置留下占位块。这里把块框之外的行按阅读顺序搬回占位块：
先找同页、排在后面的占位块，再找下一页的占位块。
搬回后，每个块的文字都位于自己的坐标范围内，并用 continues_from 串成链。
"""

import difflib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from pdftr.domain.chars import Char, CharPage
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockType, PageKind, Span
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult
from pdftr.domain.text import RICH_TAG

TOL = 3.0  # 行中心点落在块框外扩 3pt 内即视为属于该块
_BUCKET_HEIGHT = 20.0
_CHAR_LOW_MATCH = 0.3
_CHAR_HIGH_MATCH = 0.7


@dataclass
class RelocationReport:
    moved_lines: int = 0
    unmatched_lines: int = 0
    # (页, 块序号) → 它接续的 (页, 块序号)
    links: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    deduped_lines: int = 0
    deduped_blocks: int = 0


def _inside(line: LayoutLine, block: LayoutBlock) -> bool:
    x, y = line.bbox.center
    return block.bbox.contains_point(x, y, TOL)


def _char_buckets(chars: Sequence[Char]) -> dict[int, list[Char]]:
    buckets: dict[int, list[Char]] = {}
    for ch in chars:
        if ch.c.strip():
            buckets.setdefault(int(ch.bbox.center[1] // _BUCKET_HEIGHT), []).append(ch)
    return buckets


def _index_chars(char_pages: Sequence[CharPage] | None) -> dict[int, dict[int, list[Char]]]:
    if not char_pages:
        return {}
    indexed: dict[int, dict[int, list[Char]]] = {}
    for cp in char_pages:
        if cp.kind is not PageKind.NATIVE or not cp.chars:
            continue
        buckets = _char_buckets(cp.chars)
        if buckets:
            indexed[cp.index] = buckets
    return indexed


def _text_key(raw: str) -> str:
    norm = unicodedata.normalize("NFKC", RICH_TAG.sub("", raw)).lower()
    return "".join(ch for ch in norm if ch.isalnum())


def _line_text_key(line: LayoutLine) -> str:
    return _text_key("".join(span.content for span in line.spans if span.kind == "text"))


def _bands(box: Rect, tol: float = 0.0) -> range:
    return range(int((box.y0 - tol) // _BUCKET_HEIGHT), int((box.y1 + tol) // _BUCKET_HEIGHT) + 1)


def _box_chars(box: Rect, buckets: dict[int, list[Char]]) -> list[Char]:
    return [ch for band in _bands(box, TOL) for ch in buckets.get(band, ()) if box.contains_point(*ch.bbox.center, TOL)]


def _char_match_ratio(line: LayoutLine, page_buckets: dict[int, list[Char]] | None) -> float | None:
    if not page_buckets:
        return None
    wanted = _line_text_key(line)
    if len(wanted) < 3:
        return None
    hits = _box_chars(line.bbox, page_buckets)
    if not hits:
        return 0.0
    hits.sort(key=lambda c: (c.line, c.bbox.x0))
    actual = "".join(ch for ch in unicodedata.normalize("NFKC", "".join(c.c for c in hits)).lower() if ch.isalnum())
    if not actual:
        return 0.0
    matched = sum(
        block.size for block in difflib.SequenceMatcher(None, wanted, actual, autojunk=False).get_matching_blocks()
    )
    return matched / len(wanted)


def _horizontally_overlaps(a: Rect, b: Rect) -> bool:
    min_w = min(a.width, b.width)
    return min_w > 0 and (min(a.x1, b.x1) - max(a.x0, b.x0)) >= 0.2 * min_w


def _geom_cross_page(line: LayoutLine, stays: Sequence[LayoutLine]) -> bool:
    if not stays:
        return False
    prev = stays[-1]
    if _horizontally_overlaps(line.bbox, prev.bbox) and line.bbox.y0 < prev.bbox.y0 - 0.5 * max(
        line.bbox.height, prev.bbox.height
    ):
        return True
    for earlier in stays:
        if not _horizontally_overlaps(line.bbox, earlier.bbox):
            continue
        min_h = min(line.bbox.height, earlier.bbox.height)
        v_ov = min(line.bbox.y1, earlier.bbox.y1) - max(line.bbox.y0, earlier.bbox.y0)
        if min_h > 0 and v_ov > 0.5 * min_h:
            return True
    return False


type _Region = tuple[Rect, str]


def _overlapping_boxes(a: Rect, b: Rect, *, native: bool) -> bool:
    inter = a.intersection(b)
    if inter is None:
        return False
    area = inter.area / min(a.area, b.area)
    if not native:
        return area >= 0.85
    return area >= 0.70 or (
        inter.width >= 0.70 * min(a.width, b.width) and inter.height >= 0.70 * min(a.height, b.height)
    )


class _PageLines:
    """按纵向分桶保存已归属的物理行；仅在框相交时提取并缓存字形集合。"""

    def __init__(self, buckets: dict[int, list[Char]]) -> None:
        self.buckets = buckets
        self.glyphs: dict[Rect, set[int]] = {}
        self.owners: dict[int, list[_Region]] = {}

    def add(self, lines: Sequence[_Region]) -> None:
        for box, key in lines:
            for band in _bands(box):
                self.owners.setdefault(band, []).append((box, key))

    def _glyphs(self, box: Rect) -> set[int]:
        if box not in self.glyphs:
            self.glyphs[box] = {id(ch) for ch in _box_chars(box, self.buckets)}
        return self.glyphs[box]

    def contains(self, line: _Region) -> bool:
        box, key = line
        for band in _bands(box):
            for other, other_key in self.owners.get(band, ()):
                if not _overlapping_boxes(box, other, native=bool(self.buckets)):
                    continue
                if self.buckets:
                    left, right = self._glyphs(box), self._glyphs(other)
                    if left and right and len(left & right) >= 0.80 * min(len(left), len(right)):
                        return True
                elif (
                    key
                    and other_key
                    and (
                        key == other_key
                        or difflib.SequenceMatcher(None, key, other_key, autojunk=False).ratio() >= 0.85
                    )
                ):
                    return True
        return False


def _dedupe_relocated_pages(
    pages: Sequence[LayoutPage],
    links: dict[tuple[int, int], tuple[int, int]],
    char_index: dict[int, dict[int, list[Char]]],
) -> tuple[int, int]:
    linked = set(links) | set(links.values())
    removed: set[tuple[int, int]] = set()
    deduped = 0
    for page in pages:
        owners = _PageLines(char_index.get(page.index, {}))
        candidates: list[tuple[LayoutBlock, list[tuple[LayoutLine, _Region]]]] = []
        for block in page.blocks:
            if block.type is not BlockType.TEXT or block.discarded or block.placeholder:
                continue
            rows = [
                (line, (line.bbox, _line_text_key(line)))
                for line in block.lines
                if any(s.content.strip() for s in line.spans)
            ]
            if rows:
                candidates.append((block, rows))
        candidates.sort(
            key=lambda pair, pi=page.index: (
                (pi, pair[0].order) not in linked,
                -len(pair[1]),
                -sum(len(key) for _, (_, key) in pair[1]),
                pair[0].order,
            )
        )
        for block, rows in candidates:
            kept = [(line, region) for line, region in rows if not owners.contains(region)]
            deduped += len(rows) - len(kept)
            if not kept:
                removed.add((page.index, block.order))
                continue
            if len(kept) < len(rows):
                kept_ids = {id(line) for line, _ in kept}
                dropped = {id(line) for line, _ in rows} - kept_ids
                block.lines = [line for line in block.lines if id(line) not in dropped]
            owners.add([region for _, region in kept])
        page.blocks = [block for block in page.blocks if (page.index, block.order) not in removed]
    for target, source in list(links.items()):
        if target in removed or source in removed:
            del links[target]
    return deduped, len(removed)


def _block_regions(block: Block) -> list[_Region]:
    # 只比较实际有内容的行，否则块框内的留白也会吞掉真正的补漏行。
    rows: dict[int, list[Span]] = {}
    for span in block.spans:
        if span.text.strip() or span.formula:
            rows.setdefault(span.line, []).append(span)
    regions: list[_Region] = []
    for row in rows.values():
        box = Rect(
            x0=min(s.bbox.x0 for s in row),
            y0=min(s.bbox.y0 for s in row),
            x1=max(s.bbox.x1 for s in row),
            y1=max(s.bbox.y1 for s in row),
        )
        regions.append((box, _text_key("".join(s.text for s in row))))
    return regions


def dedupe_page_blocks(blocks: list[Block], page_chars: Sequence[Char] | None = None) -> list[Block]:
    """孤儿补漏后复核行归属，只移除所有内容行都有物理副本的正文块，保持阅读顺序。"""
    linked = {b.continues_from for b in blocks if b.continues_from}
    pages: dict[int, list[tuple[Block, list[_Region]]]] = {}
    for block in blocks:
        if block.type is BlockType.TEXT and block.translatable and (regions := _block_regions(block)):
            pages.setdefault(block.page, []).append((block, regions))
    removed: set[int] = set()
    # 字符序列只属于一页，混合页输入不能把这批字形用于别的页。
    buckets = _char_buckets(page_chars) if page_chars and len({b.page for b in blocks}) == 1 else {}
    for candidates in pages.values():
        owners = _PageLines(buckets)
        candidates.sort(
            key=lambda pair: (
                pair[0].orphan,
                not (pair[0].continues_from or pair[0].id in linked),
                -len(pair[1]),
                -sum(len(key) for _, key in pair[1]),
                pair[0].order,
            )
        )
        for block, regions in candidates:
            if all(owners.contains(region) for region in regions):
                removed.add(id(block))
            else:
                owners.add(regions)
    if not removed:
        return blocks
    retained = [block for block in blocks if id(block) not in removed]
    removed_ids = {b.id for b in blocks if id(b) in removed} - {b.id for b in retained}
    return [
        block.model_copy(update={"continues_from": None}) if block.continues_from in removed_ids else block
        for block in retained
    ]


def relocate(
    layout: LayoutResult,
    char_pages: Sequence[CharPage] | None = None,
) -> tuple[LayoutResult, RelocationReport]:
    result = layout.model_copy(deep=True)
    report = RelocationReport()
    pages = sorted(result.pages, key=lambda page: page.index)
    result.pages = pages
    by_index = {page.index: page for page in pages}
    char_index = _index_chars(char_pages)

    for page in pages:
        pi = page.index
        for block in page.blocks:
            if block.placeholder or not block.lines:
                continue
            candidates: list[tuple[int, LayoutBlock]] = [
                (pi, b) for b in page.blocks if b.placeholder and b.order > block.order
            ]
            following = by_index.get(pi + 1)
            if following is not None:
                candidates += [(following.index, b) for b in following.blocks if b.placeholder]
            page2 = by_index.get(pi + 2) if following is not None else None
            page2_candidates = [(page2.index, b) for b in page2.blocks if b.placeholder] if page2 is not None else []
            next_has_body = following is not None and any(
                not b.placeholder and not b.discarded and b.type is BlockType.TEXT for b in following.blocks
            )
            page2_added = False
            if page2_candidates and not next_has_body:
                candidates.extend(page2_candidates)
                page2_added = True

            has_later_candidate = any(cand_page > pi for cand_page, _ in candidates)
            if not has_later_candidate and all(_inside(ln, block) for ln in block.lines):
                continue

            stays: list[LayoutLine] = []
            prev = (pi, block.order)
            cursor = 0
            detached = False
            for line in block.lines:
                require_later_page = False
                if not detached and _inside(line, block):
                    later_pages = {
                        cand_page
                        for cand_page, cand_block in candidates[cursor:]
                        if cand_page > pi and _inside(line, cand_block)
                    }
                    if later_pages:
                        self_score = _char_match_ratio(line, char_index.get(pi))
                        cand_scores = [
                            score
                            for cand_page in later_pages
                            if (score := _char_match_ratio(line, char_index.get(cand_page))) is not None
                        ]
                        best_cand = max(cand_scores) if cand_scores else None
                        char_evidence = (
                            self_score is not None
                            and best_cand is not None
                            and self_score < _CHAR_LOW_MATCH
                            and best_cand >= _CHAR_HIGH_MATCH
                        )
                        char_disproved = (
                            self_score is not None
                            and self_score >= _CHAR_HIGH_MATCH
                            and (best_cand is None or best_cand < _CHAR_LOW_MATCH)
                        )
                        geom_evidence = _geom_cross_page(line, stays) and not char_disproved
                        if char_evidence or geom_evidence:
                            detached = True
                            require_later_page = True
                    if not detached:
                        stays.append(line)
                        continue

                # 按阅读顺序向后找第一个能容纳该行的占位块
                k = next(
                    (
                        i
                        for i in range(cursor, len(candidates))
                        if (not require_later_page or candidates[i][0] > pi) and _inside(line, candidates[i][1])
                    ),
                    None,
                )
                if k is None:
                    stays.append(line)  # 找不到归属就留在原块，宁可错位也不丢字
                    report.unmatched_lines += 1
                    continue
                cursor = k
                detached = True
                target_page, target = candidates[k]
                if not target.lines:
                    report.links[(target_page, target.order)] = prev
                    prev = (target_page, target.order)
                target.lines.append(line)
                report.moved_lines += 1
                if (
                    not page2_added
                    and page2_candidates
                    and following is not None
                    and target_page == following.index
                    and not any(
                        not b.placeholder and not b.discarded and b.type is BlockType.TEXT and b.order > target.order
                        for b in following.blocks
                    )
                ):
                    candidates.extend(page2_candidates)
                    page2_added = True
            block.lines = stays
    for page in pages:
        for b in page.blocks:
            if b.placeholder and b.lines:
                b.placeholder = False
    report.deduped_lines, report.deduped_blocks = _dedupe_relocated_pages(pages, report.links, char_index)
    return result, report
