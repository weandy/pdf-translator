"""融合入口：版面结果 + 字符 → IR Document。

步骤：续行归位 → 字符归属到块 → 逐行重建片段（文字以 PDF 为准、行内公式保留位置）
→ 统计块样式 → 用未归属字符补出解析器漏掉的文字行。
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from pdftr.domain.chars import Char, CharPage
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
from pdftr.fusion.align import align
from pdftr.fusion.formula_align import refine_block
from pdftr.fusion.relocate import relocate
from pdftr.fusion.style import block_style
from pdftr.fusion.text_fix import chars_in, choose, text_of

ORPHAN_MIN_LETTERS = 6
LABEL_GAP = 1.5  # 同一行里相距超过 1.5 个字高的文字（如横排的图例）拆成不同标签
_LABEL_WORD = re.compile(r"[A-Za-z]{3,}")  # 至少有一个 3 个字母以上的词才翻译；纯数字、L1 这类编号保留原样


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

    @property
    def coverage(self) -> float:
        return self.chars_assigned / self.chars_total if self.chars_total else 1.0


def fuse(
    job_id: str, layout: LayoutResult, char_pages: Sequence[CharPage], *, used_cloud: bool
) -> tuple[Document, FusionReport]:
    report = FusionReport()
    relocated, rel = relocate(layout)
    report.moved_lines, report.unmatched_lines = rel.moved_lines, rel.unmatched_lines
    report.continuations = len(rel.links)

    pages: list[Page] = []
    for lp, cp in zip(relocated.pages, char_pages, strict=False):
        if cp.kind is not PageKind.NATIVE:
            report.non_native_pages.append(cp.index)
        visible = [c for c in cp.chars if not c.c.isspace()]
        report.chars_total += len(visible)
        per_block, orphans = align(cp.chars, [b.bbox for b in lp.blocks])
        report.chars_assigned += len(visible) - sum(1 for c in orphans if not c.c.isspace())

        blocks: list[Block] = []
        for lb, chars in zip(lp.blocks, per_block, strict=True):
            if lb.content is not None and not lb.lines:  # 块级解析结果：先拆行、推算行内公式位置
                lb, total, found = refine_block(lb, chars)
                report.formulas_unlocated += total - found
            blocks.append(_block(cp, lb, chars, rel.links, report))
        blocks += _orphan_blocks(cp, orphans, start=len(lp.blocks), report=report)
        figure_chars = [c for lb, chars in zip(lp.blocks, per_block, strict=True) if lb.type is BlockType.FIGURE
                        for c in chars]  # fmt: skip
        blocks += _figure_labels(cp, figure_chars, start=len(blocks), report=report)
        pages.append(
            Page(index=cp.index, width=cp.width, height=cp.height, rotation=cp.rotation, kind=cp.kind, blocks=blocks)
        )

    report.pages = len(pages)
    doc = Document(job_id=job_id, pages=pages, meta=_meta(pages, layout.parser, used_cloud))
    return doc, report


def _size_from_lines(lb: LayoutBlock) -> float:
    """块里提取不到字符（如图表里用路径画出的标题）时按行高估字号。

    解析器的行框紧贴字形，高度与字号相当（论文样本里约 0.95–1.05 倍）；最大 10pt。
    """
    heights = sorted(ln.bbox.height for ln in lb.lines if ln.bbox.height > 0) or [lb.bbox.height]
    return round(min(10.0, max(5.0, heights[len(heights) // 2])), 2)


def _size_for_scan(lb: LayoutBlock) -> float:
    """扫描页只有块级坐标：先按块面积与文字长度粗估字号（字宽约 0.5、行距约 1.2 字号）得出行数，
    再按行数反推（最后一行通常不满，直接用粗估会偏大）；块框比字形略高，按每行 1.25 字号算。"""
    text = "".join(s.content for ln in lb.lines for s in ln.spans)
    width, height = lb.bbox.width, lb.bbox.height
    if not text or width <= 0 or height <= 0:
        return 10.0
    rough = (width * height / (0.6 * len(text))) ** 0.5
    lines = max(1, round(height / (1.2 * rough)))
    return round(min(max(height / (1.25 * lines), 5.0), 24.0), 2)


def _block(cp: CharPage, lb: LayoutBlock, chars: list[Char], links: dict, report: FusionReport) -> Block:
    scanned = cp.kind is PageKind.SCANNED
    style = block_style(chars, fallback_size=_size_for_scan(lb) if scanned else _size_from_lines(lb))
    spans: list[Span] = []
    formulas: list[InlineFormula] = []
    for li, line in enumerate(lb.lines):
        formula_boxes = [s.bbox for s in line.spans if s.kind == "inline_formula"]
        for s in line.spans:
            if s.kind == "inline_formula":
                key = f"m{len(formulas) + 1}"
                formulas.append(InlineFormula(key=key, latex=s.content, bbox=s.bbox, located=not scanned))
                spans.append(Span(kind="formula", formula=key, line=li, bbox=s.bbox, size=style.font_size))
                continue
            report.text_spans += 1
            pdf_text = text_of(chars_in(chars, s.bbox, exclude=formula_boxes)) if cp.kind is PageKind.NATIVE else ""
            text, replaced = choose(s.content, pdf_text)
            report.text_replaced += replaced
            if text:
                spans.append(
                    Span(text=text, line=li, bbox=s.bbox, size=style.font_size, bold=style.bold, italic=style.italic)
                )
    report.formulas += len(formulas)
    prev = links.get((cp.index, lb.order))
    return Block(
        id=block_id(cp.index, lb.order),
        page=cp.index,
        type=lb.type,
        bbox=lb.bbox,
        order=lb.order,
        spans=spans,
        formulas=formulas,
        latex=lb.latex,
        continues_from=block_id(*prev) if prev else None,
        style=style,
        translatable=lb.type in TRANSLATABLE_TYPES and not lb.discarded and any(s.text.strip() for s in spans),
    )


def _orphan_blocks(cp: CharPage, orphans: list[Char], start: int, report: FusionReport) -> list[Block]:
    """解析器漏掉的文字：按 PyMuPDF 行分组，字母足够多的行补成可翻译的文本块。"""
    by_line: dict[int, list[Char]] = defaultdict(list)
    for ch in orphans:
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
        order = start + len(blocks)
        blocks.append(
            Block(
                id=block_id(cp.index, order),
                page=cp.index,
                type=BlockType.TEXT,
                bbox=box,
                order=order,
                spans=[Span(text=text, bbox=box, size=style.font_size, bold=style.bold, italic=style.italic)],
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
        order = start + len(blocks)
        blocks.append(
            Block(
                id=block_id(cp.index, order),
                page=cp.index,
                type=BlockType.FIGURE_TEXT,
                bbox=box,
                order=order,
                spans=[Span(text=text, bbox=box, size=style.font_size, bold=style.bold, italic=style.italic)],
                style=style,
                translatable=True,
            )
        )
    report.figure_labels += len(blocks)
    return blocks


def _plain(block: Block) -> str:
    return " ".join(s.text for s in block.spans if s.kind == "text").strip()


def _meta(pages: list[Page], parser: str, used_cloud: bool) -> DocMeta:
    blocks = [b for p in pages for b in p.blocks]
    title = next((_plain(b) for b in blocks if b.type is BlockType.TITLE and _plain(b)), "")
    abstract = ""
    for i, b in enumerate(blocks):
        if b.type is BlockType.TITLE and _plain(b).strip().lower().rstrip(":") == "abstract":
            abstract = next((_plain(x) for x in blocks[i + 1 :] if x.type is BlockType.TEXT and _plain(x)), "")
            break
    if not abstract:  # 没有“Abstract”标题时，取第一段足够长的正文（S10：BERT 论文即如此）
        abstract = next((_plain(b) for b in blocks if b.type is BlockType.TEXT and len(_plain(b).split()) >= 60), "")
    return DocMeta(title=title, abstract=abstract, parser=parser, used_cloud=used_cloud)
