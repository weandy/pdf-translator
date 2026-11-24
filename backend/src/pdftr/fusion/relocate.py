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
from pdftr.domain.ir import BlockType, PageKind
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutResult
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


def _inside(line: LayoutLine, block: LayoutBlock) -> bool:
    x, y = line.bbox.center
    return block.bbox.contains_point(x, y, TOL)


def _index_chars(char_pages: Sequence[CharPage] | None) -> dict[int, dict[int, list[Char]]]:
    if not char_pages:
        return {}
    indexed: dict[int, dict[int, list[Char]]] = {}
    for cp in char_pages:
        if cp.kind is not PageKind.NATIVE or not cp.chars:
            continue
        buckets: dict[int, list[Char]] = {}
        for ch in cp.chars:
            if ch.c.isspace():
                continue
            key = int(ch.bbox.center[1] // _BUCKET_HEIGHT)
            buckets.setdefault(key, []).append(ch)
        if buckets:
            indexed[cp.index] = buckets
    return indexed


def _line_text_key(line: LayoutLine) -> str:
    raw = "".join(RICH_TAG.sub("", span.content) for span in line.spans if span.kind == "text")
    norm = unicodedata.normalize("NFKC", raw).lower()
    return "".join(ch for ch in norm if ch.isalnum())


def _char_match_ratio(line: LayoutLine, page_buckets: dict[int, list[Char]] | None) -> float | None:
    if not page_buckets:
        return None
    wanted = _line_text_key(line)
    if len(wanted) < 3:
        return None
    lo = int((line.bbox.y0 - TOL) // _BUCKET_HEIGHT)
    hi = int((line.bbox.y1 + TOL) // _BUCKET_HEIGHT)
    hits: list[Char] = []
    for bucket_idx in range(lo, hi + 1):
        for ch in page_buckets.get(bucket_idx, ()):
            if line.bbox.contains_point(*ch.bbox.center, TOL):
                hits.append(ch)
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
    return result, report
