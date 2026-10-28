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
from pdftr.fusion.relocate import relocate
from pdftr.fusion.style import block_style
from pdftr.fusion.text_fix import chars_in, choose, text_of

ORPHAN_MIN_LETTERS = 6


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
            blocks.append(_block(cp, lb, chars, rel.links, report))
        blocks += _orphan_blocks(cp, orphans, start=len(lp.blocks), report=report)
        pages.append(
            Page(index=cp.index, width=cp.width, height=cp.height, rotation=cp.rotation, kind=cp.kind, blocks=blocks)
        )

    report.pages = len(pages)
    doc = Document(job_id=job_id, pages=pages, meta=_meta(pages, layout.parser, used_cloud))
    return doc, report


def _block(cp: CharPage, lb: LayoutBlock, chars: list[Char], links: dict, report: FusionReport) -> Block:
    style = block_style(chars)
    spans: list[Span] = []
    formulas: list[InlineFormula] = []
    for li, line in enumerate(lb.lines):
        formula_boxes = [s.bbox for s in line.spans if s.kind == "inline_formula"]
        for s in line.spans:
            if s.kind == "inline_formula":
                key = f"m{len(formulas) + 1}"
                formulas.append(InlineFormula(key=key, latex=s.content, bbox=s.bbox))
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
