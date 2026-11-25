"""融合入口：版面结果 + 字符 → IR Document。

步骤：续行归位 → 字符归属到块 → 逐行重建片段（文字以 PDF 为准、行内公式保留位置）
→ 统计块样式 → 用未归属字符补出解析器漏掉的文字行。
"""

import re
import statistics
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from pdftr.domain.chars import Char, CharPage
from pdftr.domain.document_name import paper_title
from pdftr.domain.geometry import Rect
from pdftr.domain.ids import block_id
from pdftr.domain.ir import (
    TRANSLATABLE_TYPES,
    Block,
    BlockType,
    DocMeta,
    Document,
    InlineFormula,
    Page,
    PageKind,
    Span,
)
from pdftr.domain.layout import LayoutBlock, LayoutResult
from pdftr.domain.text import INLINE_ABSTRACT, PUBLICATION_DATE, RICH_TAG, SECTION_NUMBER, is_pua
from pdftr.fusion.align import align
from pdftr.fusion.emphasis import bold_ranges, italic_ranges
from pdftr.fusion.formula_align import (
    MATH_FONT,
    MATH_WORDS,
    clean_formula_lines,
    exclude_formula_chars,
    promote_native_math_spans,
    refine_block,
    visual_lines,
)
from pdftr.fusion.relocate import RelocationReport, dedupe_page_blocks, relocate
from pdftr.fusion.structure import margin_marks, split_layout
from pdftr.fusion.style import block_style
from pdftr.fusion.text_fix import (
    chars_in,
    choose,
    is_broken,
    main_metrics,
    recover_dropped_chars,
    restore_citation_superscripts,
    script_glyph,
    strip_fake_scripts,
    text_of,
)

ORPHAN_MIN_LETTERS = 6
LABEL_GAP = 1.5  # 同一行里相距超过 1.5 个字高的文字（如横排的图例）拆成不同标签
_LABEL_WORD = re.compile(r"[A-Za-z]{3,}")  # 至少有一个 3 个字母以上的词才翻译；纯数字、L1 这类编号保留原样
_CHAIN_BARRIERS = {BlockType.TITLE, BlockType.REFERENCE}
_COLUMN_TOP_BLOCKERS = {BlockType.TEXT, BlockType.TITLE, BlockType.FOOTNOTE, BlockType.REFERENCE}
_SENTENCE_END = re.compile(r"""[.?!:;。？！：；](?:\s*['"’”»）)\]}])*$""")
_CONTINUATION_START = re.compile(r"^[a-z,;:)\]，、；：）]")
_UNFINISHED = re.compile(
    r"\b(?:a|an|the|to|of|for|in|on|at|by|with|without|from|under|into|via|between|than|and|or|as|that|which|whose|its|their|our)$",
    re.I,
)
_UNFINISHED_MODIFIER = re.compile(
    r"\b(?:a|an|the|this|that|these|those|our|its|their)\s+[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$",
    re.I,
)
_STANDALONE_HEAD = re.compile(
    r"^(?:(?:abstract|key\s*words?|index\s+terms|background|objectives?|purpose|aims?|methods?|methodology|"
    r"results?|findings?|conclusions?|significance|summary|proof|remark|note|theorem|lemma|corollary|proposition|"
    r"definition|example|assumption)\b(?:\s+\d+)?\s*[.:：—–\-]|"
    r"(?:fig(?:ure)?|table|algorithm|scheme)\s+\d+(?:\.\d+)*\s*[:.]\s+\S|"
    r"[•·▪◦‣]|\(\d+\)\s|\d+[.)]\s)",
    re.I,
)
_ACRONYM_TAIL = re.compile(
    r"(?!(?:FIG(?:URE)?|TABLE|ALGORITHM|SCHEME|EQ|SEC(?:TION)?|CHAPTER|APPENDIX)\b)"
    r"[A-Z]{2,}(?:\s+[a-z]+(?:[-'][a-z]+)*){1,3}\."
)
_TOP_FRACTION = 0.35
_MAX_SIZE_GAP = 1.5
_ALGORITHM = re.compile(r"^Algorithm\s+\d+\b", re.I)
_SUBCAPTION = re.compile(r"^\([a-z]\)(?:\s|[A-Z]|$)", re.I)
_PUA_RUN = re.compile(r"[\ue000-\uf8ff\U000f0000-\U0010ffff]+")
_EQUATION_NUMBER = re.compile(r"\(\d+\)$")
_MATH_OPERATOR = re.compile(r"[=≈≃≠≤≥∞∑∫∏√±×÷∂∈∉⋅−+^<>∧∨∩∪]")


class InvalidLayoutPages(ValueError):
    """解析页号与原 PDF 不一致。"""


@dataclass
class FusionReport:
    pages: int = 0
    chars_total: int = 0
    chars_assigned: int = 0
    moved_lines: int = 0
    unmatched_lines: int = 0
    continuations: int = 0
    text_spans: int = 0
    text_replaced: int = 0
    formulas: int = 0
    orphan_blocks: int = 0
    figure_labels: int = 0
    formulas_unlocated: int = 0  # 块级解析结果里没能推算出位置的行内公式（按正文保留）
    non_native_pages: list[int] = field(default_factory=list)
    missing_layout_pages: list[int] = field(default_factory=list)
    omitted_layout_pages: list[int] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.chars_assigned / self.chars_total if self.chars_total else 1.0


def fuse(
    job_id: str,
    layout: LayoutResult,
    char_pages: Sequence[CharPage],
    *,
    used_cloud: bool,
    pages_to_translate: Iterable[int] | None = None,
    expected_layout_pages: Iterable[int] | None = None,
) -> tuple[Document, FusionReport]:
    pdf_indexes = _validate_layout(layout, char_pages)
    expected_layout = pdf_indexes if expected_layout_pages is None else (set(expected_layout_pages) & pdf_indexes)
    relocated, rel = relocate(layout, char_pages=char_pages)
    return _fuse_relocated(
        job_id,
        relocated,
        char_pages,
        rel,
        used_cloud=used_cloud,
        selected=set(pages_to_translate) if pages_to_translate is not None else pdf_indexes,
        expected_layout=expected_layout,
        restrict_meta=expected_layout_pages is not None,
    )


def _validate_layout(layout: LayoutResult, char_pages: Sequence[CharPage]) -> set[int]:
    pdf_indexes = {page.index for page in char_pages}
    seen: set[int] = set()
    for page in layout.pages:
        if not 0 <= page.index < len(char_pages) or page.index not in pdf_indexes:
            raise InvalidLayoutPages(f"解析返回的页号 {page.index} 超出 PDF 页号范围")
        if page.index in seen:
            raise InvalidLayoutPages(f"解析返回重复页号 {page.index}")
        seen.add(page.index)
    return pdf_indexes


def _fuse_relocated(
    job_id: str,
    layout: LayoutResult,
    char_pages: Sequence[CharPage],
    rel: RelocationReport,
    *,
    used_cloud: bool,
    selected: set[int],
    expected_layout: set[int],
    restrict_meta: bool,
) -> tuple[Document, FusionReport]:
    report = FusionReport()
    report.moved_lines, report.unmatched_lines = rel.moved_lines, rel.unmatched_lines

    pages: list[Page] = []
    endpoints: dict[tuple[int, int], tuple[Block, Block]] = {}
    native_chars: dict[int, list[Char]] = {}
    by_index = {page.index: page for page in layout.pages}
    native_captions: set[str] = set()
    for cp in sorted(char_pages, key=lambda page: page.index):
        if cp.kind is PageKind.NATIVE:
            native_chars[cp.index] = cp.chars
        else:
            report.non_native_pages.append(cp.index)
        visible = [c for c in cp.chars if not c.c.isspace()]
        if cp.index in expected_layout:
            report.chars_total += len(visible)
        if (lp := by_index.get(cp.index)) is None:
            can_translate = cp.kind in (PageKind.NATIVE, PageKind.SCANNED)
            if cp.index not in expected_layout:
                report.omitted_layout_pages.append(cp.index)
            elif can_translate:
                report.missing_layout_pages.append(cp.index)
            pages.append(
                Page(index=cp.index, width=cp.width, height=cp.height, rotation=cp.rotation, kind=cp.kind,
                     missing_layout=can_translate and cp.index in selected and cp.index in expected_layout
                     and (bool(visible) or cp.has_images))
            )  # fmt: skip
            continue
        per_block, orphans = align(cp.chars, [b.bbox for b in lp.blocks])
        if cp.index in expected_layout:
            report.chars_assigned += len(visible) - sum(1 for c in orphans if not c.c.isspace())

        blocks: list[Block] = []
        marks = margin_marks(orphans) if cp.kind is PageKind.NATIVE else []
        kept_on_background: set[int] = set()
        inserted = 0
        for lb, chars in sorted(zip(lp.blocks, per_block, strict=True), key=lambda pair: pair[0].order):
            if lb.content is not None and not lb.lines:  # 块级解析结果：先拆行、推算行内公式位置
                lb, total, found = refine_block(lb, chars)
                report.formulas_unlocated += total - found
            elif any(s.kind == "inline_formula" for line in lb.lines for s in line.spans):
                lb = clean_formula_lines(lb, chars)
            if cp.kind is PageKind.NATIVE and lb.type is BlockType.TEXT and chars:
                promoted = promote_native_math_spans(lb, chars)
                if promoted is not lb:
                    lb = clean_formula_lines(promoted, chars)
            first = len(blocks)
            pieces, matched = split_layout(lb, chars, marks, cp.width)
            if matched:
                kept_on_background.update(matched)
                marks = [mark for mark in marks if id(mark.chars[0]) not in matched]
            for i, (piece, glyphs, list_item) in enumerate(pieces):
                order = lb.order + inserted + i
                if piece.order != order:
                    piece = piece.model_copy(update={"order": order})
                block = _block(cp, piece, glyphs, report, list_item=list_item)
                blocks.append(block)
                if block.type is BlockType.CAPTION and any(not c.c.isspace() for c in glyphs):
                    native_captions.add(block.id)
            endpoints[cp.index, lb.order] = (blocks[first], blocks[-1])
            inserted += len(pieces) - 1
        if kept_on_background:
            orphans = [c for c in orphans if id(c) not in kept_on_background]
        _reorder_displaced_titles(blocks)
        protected_captions = _protect_layout_scope(blocks)
        start = max((b.order for b in blocks), default=-1) + 1
        if cp.kind is PageKind.NATIVE:
            protected, orphans = _orphan_formulas(cp, orphans, start)
            blocks.extend(protected)
            start += len(protected)
        missing = _orphan_blocks(cp, orphans, start=start, report=report)
        blocks.extend(missing)
        figure_chars = [
            c for lb, chars in zip(lp.blocks, per_block, strict=True) if lb.type is BlockType.FIGURE
            for c in chars if not any(box.contains_point(*c.bbox.center) for box in protected_captions)
        ]  # fmt: skip
        blocks += _figure_labels(cp, figure_chars, start=start + len(missing), report=report)
        pages.append(
            Page(index=cp.index, width=cp.width, height=cp.height, rotation=cp.rotation, kind=cp.kind, blocks=blocks)
        )

    for target, source in rel.links.items():
        nxt, prev = endpoints[target][0], endpoints[source][1]
        if not nxt.list_item:
            nxt.continues_from = prev.id
            report.continuations += 1
    for page in pages:
        page.blocks = dedupe_page_blocks(page.blocks, native_chars.get(page.index))
    report.continuations += _supplement_continuations(pages)
    _even_scan_sizes(pages)
    _even_caption_sizes(pages, native_captions)
    report.pages = len(pages)
    meta = _meta(pages, layout.parser, used_cloud, expected_layout if restrict_meta else None)
    doc = Document(job_id=job_id, pages=pages, meta=meta)
    return doc, report


def _is_short_title(block: Block) -> bool:
    if block.type is not BlockType.TITLE or not block.translatable:
        return False
    if len({s.line for s in block.spans}) > 2:
        return False
    plain = RICH_TAG.sub("", _plain(block)).strip()
    return 0 < len(plain) <= 120 and len(plain.split()) <= 15


def _reorder_displaced_titles(blocks: list[Block]) -> None:
    """轻量同栏几何纠序：短 TITLE 位于同栏 TEXT 正上方但在阅读序中排在其后时，前移至紧邻其下方的 TEXT 之前。"""
    if len(blocks) < 2:
        return
    orders = sorted(b.order for b in blocks)
    moved = False
    for title in [b for b in blocks if _is_short_title(b)]:
        idx = blocks.index(title)
        tbox = title.bbox
        below: list[tuple[int, Block]] = []
        for j in range(idx):
            cand = blocks[j]
            if cand.type is not BlockType.TEXT:
                continue
            cbox = cand.bbox
            min_w = min(tbox.width, cbox.width)
            if min_w <= 0:
                continue
            h_ov = min(tbox.x1, cbox.x1) - max(tbox.x0, cbox.x0)
            if h_ov >= 0.5 * min_w and cbox.y0 >= tbox.y1 - 5.0 and cbox.center[1] > tbox.center[1]:
                below.append((j, cand))
        if not below:
            continue
        top_below = min((cand for _, cand in below), key=lambda b: b.bbox.y0)
        if not -5.0 <= top_below.bbox.y0 - tbox.y1 <= 30.0:
            continue
        target = min(j for j, _ in below)
        blocks.pop(idx)
        blocks.insert(target, title)
        moved = True
    if moved:
        for block, order in zip(blocks, orders, strict=True):
            block.order = order


def _column(block: Block, page: Page) -> int:
    mid = page.width / 2
    if block.bbox.x0 >= mid - block.style.font_size:
        return 1
    return 0 if block.bbox.x1 <= mid + block.style.font_size else -1


def _indented(block: Block, peers: Sequence[Block]) -> bool:
    starts: dict[int, float] = {}
    for span in block.spans:
        starts[span.line] = min(starts.get(span.line, span.bbox.x0), span.bbox.x0)
    if not starts:
        return False
    first = starts[min(starts)]
    margin = min(block.bbox.x0, *starts.values(), *(peer.bbox.x0 for peer in peers))
    return first - margin > 0.8 * block.style.font_size


def _is_column_top(nxt: Block, page: Page) -> bool:
    """页顶或在当前栏内上方无更早正文/标题/脚注/参考文献块（上方仅有浮动图表/图注/页眉）时，视作栏顶首块。"""
    if nxt.bbox.y0 <= _TOP_FRACTION * page.height:
        return True
    for other in page.blocks:
        if other.id == nxt.id or other.type not in _COLUMN_TOP_BLOCKERS:
            continue
        if (
            other.bbox.x0 < nxt.bbox.x1
            and nxt.bbox.x0 < other.bbox.x1
            and (other.bbox.y1 <= nxt.bbox.y0 + 2.0 or other.bbox.center[1] < nxt.bbox.y0)
        ):
            return False
    return True


def _strong_unfinished(tail_plain: str) -> bool:
    return bool(
        _UNFINISHED.search(tail_plain)
        or _UNFINISHED_MODIFIER.search(tail_plain)
        or tail_plain.endswith(("-", "\u00ad", "_", "–", "—", ","))
        or tail_plain.count("(") > tail_plain.count(")")
        or tail_plain.count("[") > tail_plain.count("]")
    )


def _has_same_column_barrier(page: Page, prev: Block, nxt: Block) -> bool:
    lo_x = min(prev.bbox.x0, nxt.bbox.x0)
    hi_x = max(prev.bbox.x1, nxt.bbox.x1)
    lo_y = prev.bbox.y1 - 2.0
    hi_y = nxt.bbox.y0 + 2.0
    for other in page.blocks:
        if other.id in {prev.id, nxt.id}:
            continue
        if other.bbox.x0 < hi_x and other.bbox.x1 > lo_x and lo_y < other.bbox.center[1] < hi_y:
            return True
    return False


def _supplement_continuations(pages: list[Page]) -> int:
    """补上缺少占位块的续接（跨页、跨栏、绕图与同栏相邻块），每个块最多一个后继，标题和参考文献截断搜索。"""
    blocks = {b.id: b for p in pages for b in p.blocks}
    used = {b.continues_from for b in blocks.values() if b.continues_from is not None}
    text = {
        b.id: RICH_TAG.sub("", _plain(b)).strip()
        for b in blocks.values()
        if b.type is BlockType.TEXT and b.translatable and not b.orphan
    }
    groups: dict[str, int] = {}
    page_of = {p.index: p for p in pages}
    formulas = {p.index: [b for b in p.blocks if b.type is BlockType.FORMULA and b.orphan] for p in pages}
    group = 0
    for page in pages:
        for b in sorted(page.blocks, key=lambda b: b.order):
            if b.type in _CHAIN_BARRIERS:
                group += 1
            groups[b.id] = group

    def eligible(prev: Block, nxt: Block) -> bool:
        def same_column(a: Block, b: Block) -> bool:
            return a.bbox.x0 < b.bbox.x1 and b.bbox.x0 < a.bbox.x1

        prev_barrier = any(same_column(b, prev) and b.bbox.y0 >= prev.bbox.y1 for b in formulas[prev.page])
        next_barrier = any(same_column(b, nxt) and b.bbox.y1 <= nxt.bbox.y0 for b in formulas[nxt.page])
        # 跨栏与跨页的自然阅读边界分别取前栏末尾和后栏开头。
        across = prev.page != nxt.page or _column(prev, page_of[prev.page]) != _column(nxt, page_of[nxt.page])
        return (
            prev.id not in used
            and bool(text[prev.id])
            and not (prev.list_item and PUBLICATION_DATE.match(text[prev.id]))
            and not _SENTENCE_END.search(text[prev.id])
            and abs(prev.style.font_size - nxt.style.font_size) <= _MAX_SIZE_GAP
            and not (across and (prev_barrier or next_barrier))
        )

    def link(prev: Block, nxt: Block) -> bool:
        # 已有归位链可能跨页或倒序，沿前驱检查避免新边闭环。
        ancestor: Block | None = prev
        seen = {nxt.id}
        while ancestor is not None:
            if ancestor.id in seen:
                return False
            seen.add(ancestor.id)
            ancestor = blocks.get(ancestor.continues_from or "")
        nxt.continues_from = prev.id
        used.add(prev.id)
        return True

    count = 0
    previous: Block | None = None
    for page in pages:
        body = [b for b in sorted(page.blocks, key=lambda b: b.order) if text.get(b.id)]
        left = [b for b in body if _column(b, page) == 0]
        right = [b for b in body if _column(b, page) == 1]
        first_column = [b for b in body if _column(b, page) != 1]
        first = min(first_column, key=lambda b: b.bbox.y0, default=None)
        if first is None and right:
            first = min(right, key=lambda b: b.bbox.y0)
            first_peers = right
        else:
            first_peers = left if first is not None and _column(first, page) == 0 else first_column
        right_top = min(right, key=lambda b: b.bbox.y0, default=None) if left else None
        for nxt, peers in ((first, first_peers), (right_top, right)):
            if (
                nxt is None
                or nxt.list_item
                or nxt.continues_from
                or not _is_column_top(nxt, page)
                or _indented(nxt, peers)
            ):
                continue
            if nxt is first:
                prev = previous if previous is not None and previous.page + 1 == page.index else None
            else:
                prev = next((b for b in reversed(body) if b.order < nxt.order), None)
                if prev is not None and prev not in left:
                    prev = None
            tail = text[prev.id] if prev is not None else ""
            head = text[nxt.id]
            continuation = (
                _CONTINUATION_START.match(head)
                or (_strong_unfinished(tail) and not _STANDALONE_HEAD.match(head))
                or _ACRONYM_TAIL.fullmatch(head)
            )
            if (
                prev is not None and groups[prev.id] == groups[nxt.id]
                and eligible(prev, nxt) and continuation and link(prev, nxt)
            ):  # fmt: skip
                count += 1
            if nxt is right_top and nxt.continues_from is None and re.match(r"[a-z]", head):
                # 解析顺序已倒置，按左栏内实际位于候选后方的结构块截断。
                barriers = [b for b in page.blocks if b.type in _CHAIN_BARRIERS and _column(b, page) == 0]
                candidates = [
                    b
                    for b in left
                    if b.order > nxt.order and eligible(b, nxt) and not any(h.bbox.y0 > b.bbox.y0 for h in barriers)
                ]
                if len(candidates) == 1 and link(candidates[0], nxt):
                    count += 1

        # 同栏紧邻两 TEXT 块续接：无阻隔、间距正常、前块未收尾且后块无首行缩进、非列表项。
        by_col: dict[int, list[Block]] = defaultdict(list)
        for b in body:
            by_col[_column(b, page)].append(b)
        for col_blocks in by_col.values():
            col_blocks.sort(key=lambda b: (b.bbox.y0, b.order))
            for prev, nxt in pairwise(col_blocks):
                if (
                    prev.order >= nxt.order
                    or nxt.list_item
                    or nxt.continues_from is not None
                    or groups[prev.id] != groups[nxt.id]
                ):
                    continue
                min_w = min(prev.bbox.width, nxt.bbox.width)
                h_ov = min(prev.bbox.x1, nxt.bbox.x1) - max(prev.bbox.x0, nxt.bbox.x0)
                max_size = max(prev.style.font_size, nxt.style.font_size)
                v_gap = nxt.bbox.y0 - prev.bbox.y1
                if (
                    min_w <= 0
                    or h_ov < 0.3 * min_w
                    or not -0.5 * max_size <= v_gap <= 2.5 * max_size
                    or nxt.bbox.x0 - prev.bbox.x0 > 0.8 * prev.style.font_size
                    or _indented(nxt, col_blocks)
                    or _has_same_column_barrier(page, prev, nxt)
                    or not eligible(prev, nxt)
                ):
                    continue
                tail = text[prev.id]
                head = text[nxt.id]
                if _STANDALONE_HEAD.match(head):
                    continue
                same_col_cont = _strong_unfinished(tail) or (
                    bool(_CONTINUATION_START.match(head)) and len(tail.split()) >= 3
                )
                if same_col_cont and link(prev, nxt):
                    count += 1

        previous = body[-1] if body else None
    return count


def _even_scan_sizes(pages: list[Page]) -> None:
    """扫描页的字号是逐块估的，短块误差大，同一页正文忽大忽小。论文正文字号基本统一，
    扫描页上的正文块统一用全文正文估计值的中位数（标题、图注、脚注保留各自的估计）。"""
    body = [b for p in pages if p.kind is PageKind.SCANNED for b in p.blocks if b.type is BlockType.TEXT]
    if len(body) < 3:
        return
    size = round(statistics.median(b.style.font_size for b in body), 2)
    for b in body:
        b.style.font_size = size
        for span in b.spans:
            span.size = size


def _protect_layout_scope(blocks: list[Block]) -> list[Rect]:
    figures = [b.bbox for b in blocks if b.type is BlockType.FIGURE]
    code = [b.bbox for b in blocks if b.type is BlockType.CODE]
    protected_captions: list[Rect] = []
    for block in blocks:
        box = block.bbox
        if block.type is BlockType.CAPTION and any(
            (overlap := box.intersection(figure)) is not None and overlap.area >= 0.5 * box.area for figure in figures
        ):
            block.translatable = False
            protected_captions.append(box)
        if not block.translatable:
            continue
        text = RICH_TAG.sub("", _plain(block))
        if (
            block.type in {BlockType.TEXT, BlockType.TITLE, BlockType.CAPTION}
            and len(text) <= 180
            and len({span.line for span in block.spans}) <= 2
            and _ALGORITHM.match(text)
            and any(c.x0 - 6 <= box.x0 and box.x1 <= c.x1 + 6 and -8 <= c.y0 - box.y1 <= 12 for c in code)
        ):
            block.translatable = False
    return protected_captions


def _even_caption_sizes(pages: list[Page], backed: set[str]) -> None:
    """用同页真实字号校准由路径绘制的图外子图题。"""
    for page in pages:
        captions = [b for b in page.blocks if b.type is BlockType.CAPTION and b.translatable]
        measured = [b for b in captions if b.id in backed]
        if not measured:
            continue
        for caption in captions:
            if caption.id in backed or not _SUBCAPTION.match(RICH_TAG.sub("", _plain(caption))):
                continue
            siblings = [b for b in measured if abs(b.bbox.center[1] - caption.bbox.center[1]) <= b.style.font_size]
            size = round(statistics.median(b.style.font_size for b in siblings or measured), 2)
            caption.style.font_size = size
            for span in caption.spans:
                span.size = size


def _size_from_lines(lb: LayoutBlock) -> float:
    """块里提取不到字符（如图表里用路径画出的标题）时按行高估字号。

    解析器的行框紧贴字形，高度与字号相当（论文样本里约 0.95–1.05 倍）；最大 10pt。
    """
    heights = sorted(ln.bbox.height for ln in lb.lines if ln.bbox.height > 0) or [lb.bbox.height]
    return round(min(10.0, max(5.0, heights[len(heights) // 2])), 2)


def _size_for_scan(lb: LayoutBlock) -> float:
    """扫描页只有块级坐标：先按块面积与文字长度粗估字号（字宽约 0.5、行距约 1.2 字号）得出行数，
    再按行数反推（最后一行通常不满，直接用粗估会偏大）；块框比字形略高，按每行 1.25 字号算。"""
    if lb.type is BlockType.TITLE and lb.lines:
        line_heights = [line.bbox.height for line in lb.lines if line.bbox.height > 0]
        if line_heights:
            return round(min(max(statistics.median(line_heights) * 0.95, 5.0), 24.0), 2)
    text = "".join(s.content for ln in lb.lines for s in ln.spans)
    width, height = lb.bbox.width, lb.bbox.height
    if not text or width <= 0 or height <= 0:
        return 10.0
    rough = (width * height / (0.6 * len(text))) ** 0.5
    lines = max(1, round(height / (1.2 * rough)))
    return round(min(max(height / (1.25 * lines), 5.0), 24.0), 2)


def _glyph_box(chars: Sequence[Char]) -> Rect:
    return Rect.of(
        (
            min(c.bbox.x0 for c in chars),
            min(c.bbox.y0 for c in chars),
            max(c.bbox.x1 for c in chars),
            max(c.bbox.y1 for c in chars),
        )
    )


def _pua_fragments(text: str, chars: list[Char]) -> list[tuple[str, list[Char], bool]] | None:
    """原文与字形中的私用字串数量一一对应且顺序可唯一定位时才裁图。"""
    glyphs = sorted(chars, key=lambda c: (c.line, c.bbox.x0))
    raw = "".join(c.c for c in glyphs)
    if len(raw) != len(glyphs):
        return None
    fragments: list[tuple[str, list[Char], bool]] = []
    start = glyph_start = 0
    for run in _PUA_RUN.finditer(text):
        if len(run[0]) > 4 or text.count(run[0]) != raw.count(run[0]):
            return None
        pos = raw.find(run[0], glyph_start)
        if pos < 0:
            return None
        end = pos + len(run[0])
        selected = glyphs[pos:end]
        # 跨脚本或强调的私用字不能拆开其成对标签。
        tags = list(RICH_TAG.finditer(text, start, run.start()))
        if sum(-1 if tag[0].startswith("</") else 1 for tag in tags):
            return None
        box = _glyph_box(selected)
        if (
            pos < glyph_start or box.area <= 0 or len({c.line for c in selected}) != 1
            or any(c.bbox.x0 - p.bbox.x1 > max(c.size, p.size) * 0.5 for p, c in pairwise(selected))
            or any(box.contains_point(*c.bbox.center) for c in glyphs[:pos] + glyphs[end:] if not c.c.isspace())
        ):  # fmt: skip
            return None
        fragments.extend(((text[start : run.start()], glyphs[glyph_start:pos], False), ("", selected, True)))
        start, glyph_start = run.end(), end
    fragments.append((text[start:], glyphs[glyph_start:], False))
    return fragments


def _block(cp: CharPage, lb: LayoutBlock, chars: list[Char], report: FusionReport, *, list_item: bool) -> Block:
    scanned = cp.kind is PageKind.SCANNED
    style = block_style(
        chars,
        fallback_size=_size_for_scan(lb) if scanned else _size_from_lines(lb),
        title=lb.type is BlockType.TITLE,
    )
    spans: list[Span] = []
    formulas: list[InlineFormula] = []
    unresolved = False
    line_centers = [line.bbox.center[1] for line in lb.lines]
    for li, line in enumerate(lb.lines):
        formula_boxes = [s.bbox for s in line.spans if s.kind == "inline_formula" and s.located]
        line_native = (
            [c for c in chars_in(chars, line.bbox, tol=1.0) if not c.c.isspace()]
            if cp.kind is PageKind.NATIVE and formula_boxes
            else []
        )
        body_size, line_baseline = main_metrics(line_native) if line_native else (style.font_size, None)
        text_candidates = [s.bbox for s in line.spans if s.kind == "text"]
        rightmost_span = max(line.spans, key=lambda sp: sp.bbox.x1, default=None)
        for s in line.spans:
            if s.kind == "inline_formula":
                key = f"m{len(formulas) + 1}"
                formulas.append(InlineFormula(key=key, latex=s.content, bbox=s.bbox, located=s.located and not scanned))
                spans.append(Span(kind="formula", formula=key, line=li, bbox=s.bbox, size=style.font_size))
                continue
            report.text_spans += 1
            search_box = s.bbox
            if cp.kind is PageKind.NATIVE and chars and s is rightmost_span:
                overflow_x1 = max(
                    (
                        ch.bbox.x1
                        for ch in chars
                        if not ch.c.isspace()
                        and ch.bbox.x1 > s.bbox.x1
                        and ch.bbox.center[0] >= s.bbox.x1 - 1.0
                        and s.bbox.y0 - 1.0 <= ch.bbox.center[1] <= s.bbox.y1 + 1.0
                        and not any(e.contains_point(*ch.bbox.center, 0.5) for e in formula_boxes)
                        and min(range(len(line_centers)), key=lambda idx: abs(ch.bbox.center[1] - line_centers[idx]))
                        == li
                    ),
                    default=s.bbox.x1,
                )
                if overflow_x1 > s.bbox.x1:
                    # 仅扩展单行 Span.bbox 供背景去字擦除溢出尾字，不再撑大整块排版框 Block.bbox。
                    search_box = s.bbox.model_copy(update={"x1": overflow_x1})
            span_chars = (
                exclude_formula_chars(chars_in(chars, search_box), formula_boxes, body_size, line_baseline)
                if cp.kind is PageKind.NATIVE
                else []
            )
            pdf_text = text_of(span_chars)
            stripped = strip_fake_scripts(s.content, span_chars)
            recovered = recover_dropped_chars(stripped, pdf_text)
            text, replaced = choose(recovered, pdf_text)
            if cp.kind is PageKind.NATIVE and span_chars:
                restored_sup = restore_citation_superscripts(text, span_chars)
                if restored_sup != text:
                    text = restored_sup
                    replaced = True
            report.text_replaced += replaced or recovered != stripped
            fragments = [(text, span_chars, False)]
            if _PUA_RUN.search(text):
                cropped = _pua_fragments(text, span_chars)
                if cropped is not None and any(
                    not search_box.contains_point(*c.bbox.center)
                    or sum(box.contains_point(*c.bbox.center) for box in text_candidates) != 1
                    for _, glyphs, crop in cropped if crop for c in glyphs
                ):  # fmt: skip
                    cropped = None
                if cropped is None:
                    unresolved = True
                else:
                    fragments = cropped
            for piece, piece_chars, crop in fragments:
                box = _glyph_box(piece_chars) if len(fragments) > 1 and piece_chars else search_box
                if crop:
                    key = f"m{len(formulas) + 1}"
                    formulas.append(InlineFormula(key=key, latex="", bbox=box, located=True))
                    spans.append(Span(kind="formula", formula=key, line=li, bbox=box, size=style.font_size))
                    continue
                if not piece.strip():
                    continue
                plain = RICH_TAG.sub("", piece).strip()
                if plain and all(c.isspace() or is_broken(c) for c in plain):
                    if all(c.isspace() or unicodedata.category(c) in {"Cc", "Cf"} for c in plain):
                        continue
                    unresolved = True
                bold, ranges = bold_ranges(piece, piece_chars)
                italic, italic_ranges_ = italic_ranges(piece, piece_chars)
                span_style = block_style(piece_chars, fallback_size=style.font_size, title=lb.type is BlockType.TITLE)
                spans.append(
                    Span(
                        text=piece,
                        line=li,
                        bbox=box,
                        size=span_style.font_size,
                        bold=bold,
                        bold_ranges=ranges,
                        italic=italic,
                        italic_ranges=italic_ranges_,
                    )
                )
    report.formulas += len(formulas)
    return Block(
        id=block_id(cp.index, lb.order),
        page=cp.index,
        type=lb.type,
        level=lb.level,
        bbox=lb.bbox,
        order=lb.order,
        spans=spans,
        formulas=formulas,
        latex=lb.latex,
        list_item=list_item,
        style=style,
        translatable=(
            lb.type in TRANSLATABLE_TYPES and not lb.discarded and not unresolved and any(s.text.strip() for s in spans)
        ),
    )


def _math_reference_metrics(chars: list[Char]) -> tuple[float, float | None]:
    """上下标密集的公式行里小字可能超过半数；按较大主簇确定主字号和基线，避免中位数掉进下标。"""
    sizes = sorted(c.size for c in chars)
    size, baseline = main_metrics(chars)
    high = sizes[min(len(sizes) - 1, int(len(sizes) * 0.75))]
    if high >= 1.15 * size:
        major = [c for c in chars if 0.88 * high < c.size <= 1.14 * high]
        if major:
            size = statistics.median(c.size for c in major)
            bases = [c.origin_y for c in major if c.origin_y is not None]
            baseline = statistics.median(bases) if bases else None
    return size, baseline


def _math_region(chars: list[Char], numbered: bool) -> bool:
    if not chars or any(is_broken(c.c) and not is_pua(c.c) for c in chars):
        return False
    size, baseline = _math_reference_metrics(chars)
    math_fonts = sum(bool(MATH_FONT.search(c.font)) for c in chars) >= 0.55 * len(chars)
    text = "".join(c.c for c in chars)
    operator = bool(_MATH_OPERATOR.search(text))
    unpaired = text.count("(") != text.count(")") or text.count("[") != text.count("]")
    alnum_scripts = [script_glyph(c, size, baseline) for c in chars if c.c.isalnum()]
    jump = any(alnum_scripts) and not all(alnum_scripts)
    ordinary = 0
    for match in re.finditer(r"[A-Za-z]+", text):
        word = chars[match.start() : match.end()]
        # rawdict 可能省略词间空格，同时用字形间距断开。
        runs: list[list[Char]] = [[]]
        for glyph in word:
            if runs[-1] and glyph.bbox.x0 - runs[-1][-1].bbox.x1 > 0.25 * size:
                runs.append([])
            runs[-1].append(glyph)
        for run in runs:
            scripts = [script_glyph(c, size, baseline) for c in run]
            token = "".join(c.c for c in run)
            if (
                len(token) >= 3
                and not any(scripts)
                and token.lower() not in MATH_WORDS
                and not (token.isupper() and len(token) <= 3)
            ):
                ordinary += 1
    return ordinary < 2 and (
        (numbered and (operator or jump or math_fonts or unpaired))
        or (math_fonts and (operator or jump or unpaired))
        or (jump and operator)
    )


def _orphan_formulas(cp: CharPage, orphans: list[Char], start: int) -> tuple[list[Block], list[Char]]:
    """按基线重建数学区域，保留原像素并阻挡译文下溢。"""
    blocks: list[Block] = []
    used: set[int] = set()
    for row in visual_lines([c for c in orphans if c.upright]):
        visible = [c for c in row if not c.c.isspace()]
        if len(visible) < 3:
            continue
        size, baseline = _math_reference_metrics(visible)
        runs: list[list[Char]] = [[visible[0]]]
        for left, right in pairwise(visible):
            if right.bbox.x0 - left.bbox.x1 > max(4 * size, cp.width * 0.06):
                runs.append([])
            runs[-1].append(right)
        # 式号可以是 PyMuPDF 的另一行，必须与主体共基线且位于主体右侧。
        number = runs[-1]
        numbered = (
            len(runs) >= 2
            and _EQUATION_NUMBER.fullmatch("".join(c.c for c in number)) is not None
            and all(not script_glyph(c, size, baseline) for c in number)
        )
        body_all = [c for run in runs[:-1] for c in run]
        regions = [(visible, body_all, True)] if numbered else [(run, run, False) for run in runs]
        for region, body, with_number in regions:
            if not _math_region(body, with_number):
                continue
            order = start + len(blocks)
            blocks.append(
                Block(
                    id=block_id(cp.index, order),
                    page=cp.index,
                    type=BlockType.FORMULA,
                    bbox=_glyph_box(region),
                    order=order,
                    style=block_style(region),
                    translatable=False,
                    orphan=True,
                )
            )
            used.update(id(c) for c in region)
    return blocks, [c for c in orphans if id(c) not in used]


def _orphan_blocks(cp: CharPage, orphans: list[Char], start: int, report: FusionReport) -> list[Block]:
    """解析器漏掉的文字：按 PyMuPDF 行分组，字母足够多的行补成可翻译的文本块。"""
    by_line: dict[int, list[Char]] = defaultdict(list)
    for ch in orphans:
        if ch.upright:
            by_line[ch.line].append(ch)
    blocks: list[Block] = []
    for chars in by_line.values():
        text = text_of(chars)
        if len(re.findall(r"[A-Za-z]", text)) < ORPHAN_MIN_LETTERS or len(text.split()) < 2:
            continue
        box = Rect(
            x0=min(c.bbox.x0 for c in chars),
            y0=min(c.bbox.y0 for c in chars),
            x1=max(c.bbox.x1 for c in chars),
            y1=max(c.bbox.y1 for c in chars),
        )
        style = block_style(chars)
        bold, ranges = bold_ranges(text, chars)
        italic, italic_ranges_ = italic_ranges(text, chars)
        order = start + len(blocks)
        blocks.append(
            Block(
                id=block_id(cp.index, order),
                page=cp.index,
                type=BlockType.TEXT,
                level=None,
                bbox=box,
                order=order,
                spans=[
                    Span(
                        text=text,
                        bbox=box,
                        size=style.font_size,
                        bold=bold,
                        bold_ranges=ranges,
                        italic=italic,
                        italic_ranges=italic_ranges_,
                    )
                ],
                style=style,
                translatable=True,
                orphan=True,
            )
        )
    report.orphan_blocks += len(blocks)
    return blocks


def _label_runs(chars: list[Char]) -> list[list[Char]]:
    """按 PyMuPDF 行分组，再在大间隔处断开；只取水平排列的行。"""
    by_line: dict[int, list[Char]] = defaultdict(list)
    for ch in chars:
        if ch.upright:
            by_line[ch.line].append(ch)
    runs: list[list[Char]] = []
    for line in by_line.values():
        line.sort(key=lambda c: c.bbox.x0)
        run: list[Char] = []
        last: Char | None = None  # 上一个可见字符
        for ch in line:
            if not ch.c.isspace():
                if last and ch.bbox.x0 - last.bbox.x1 > max(ch.size, last.size) * LABEL_GAP:
                    runs.append(run)
                    run = []
                last = ch
            run.append(ch)
        runs.append(run)
    return runs


def _figure_labels(cp: CharPage, chars: list[Char], start: int, report: FusionReport) -> list[Block]:
    """图里的矢量文字（坐标轴标题、图例、框图标签）拆成可翻译的小块；位图里的文字提取不到，不处理。"""
    blocks: list[Block] = []
    for run in _label_runs(chars):
        visible = [c for c in run if not c.c.isspace()]
        text = text_of(run).strip()
        if not visible or not _LABEL_WORD.search(text):
            continue
        box = Rect(
            x0=min(c.bbox.x0 for c in visible),
            y0=min(c.bbox.y0 for c in visible),
            x1=max(c.bbox.x1 for c in visible),
            y1=max(c.bbox.y1 for c in visible),
        )
        style = block_style(visible)
        bold, ranges = bold_ranges(text, run)
        italic, italic_ranges_ = italic_ranges(text, run)
        order = start + len(blocks)
        blocks.append(
            Block(
                id=block_id(cp.index, order),
                page=cp.index,
                type=BlockType.FIGURE_TEXT,
                level=None,
                bbox=box,
                order=order,
                spans=[
                    Span(
                        text=text,
                        bbox=box,
                        size=style.font_size,
                        bold=bold,
                        bold_ranges=ranges,
                        italic=italic,
                        italic_ranges=italic_ranges_,
                    )
                ],
                style=style,
                translatable=True,
            )
        )
    report.figure_labels += len(blocks)
    return blocks


def _plain(block: Block) -> str:
    return " ".join(s.text for s in block.spans if s.kind == "text").strip()


def _meta(pages: list[Page], parser: str, used_cloud: bool, expected_layout: set[int] | None = None) -> DocMeta:
    blocks = [b for p in pages for b in p.blocks]
    title = paper_title(pages)
    abstract = ""
    for i, b in enumerate(blocks):
        text = RICH_TAG.sub("", _plain(b))
        if b.type is BlockType.TEXT and (lead := INLINE_ABSTRACT.match(text)) and text[:8].casefold() == "abstract":
            abstract = text[lead.end() :].strip()
            if abstract:
                break
        heading = "".join(SECTION_NUMBER.sub("", text).split()).casefold().rstrip(":：")
        if b.type is BlockType.TITLE and heading == "abstract":
            candidates = blocks[i + 1 :]
            if expected_layout is not None:
                candidates = [x for x in candidates if x.page in (b.page, b.page + 1)]
            abstract = next((_plain(x) for x in candidates if x.type is BlockType.TEXT and _plain(x)), "")
            break
    if not abstract:  # 没有“Abstract”标题时，取第一段足够长的正文（S10：BERT 论文即如此）
        candidates = (
            blocks if expected_layout is None else [b for b in blocks if b.page in (0, 1) and b.page in expected_layout]
        )
        abstract = next(
            (_plain(b) for b in candidates if b.type is BlockType.TEXT and len(_plain(b).split()) >= 60), ""
        )
    return DocMeta(title=title, abstract=abstract, parser=parser, used_cloud=used_cloud)
