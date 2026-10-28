"""MinerU layout.json → LayoutResult。

依据 S2/S4 对 5 篇论文的实测：
- 坐标为 PDF point（与 page_size 一致），直接使用。
- 图、表、图表、代码块带子块（body / caption / footnote），展开成独立的块。
- 跨栏/跨页段落：续行被并进前一块，原位置留下 lines 为空、lines_deleted 为真的占位块。
- discarded_blocks 里的页眉、页码、侧边标识不展示；page_footnote 是正文脚注，要保留并翻译。
"""

from typing import Any

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan

_TOP_TYPES = {
    "text": BlockType.TEXT,
    "title": BlockType.TITLE,
    "interline_equation": BlockType.FORMULA,
    "ref_text": BlockType.REFERENCE,
    "list": BlockType.TEXT,
    "index": BlockType.TEXT,
}

_CHILD_TYPES = {
    "image_body": BlockType.FIGURE,
    "chart_body": BlockType.FIGURE,
    "table_body": BlockType.TABLE,
    "code_body": BlockType.CODE,
    "image_caption": BlockType.CAPTION,
    "chart_caption": BlockType.CAPTION,
    "table_caption": BlockType.CAPTION,
    "code_caption": BlockType.CAPTION,
    "image_footnote": BlockType.FOOTNOTE,
    "chart_footnote": BlockType.FOOTNOTE,
    "table_footnote": BlockType.FOOTNOTE,
}

_DISCARDED_TYPES = {
    "header": (BlockType.HEADER, True),
    "footer": (BlockType.FOOTER, True),
    "page_number": (BlockType.FOOTER, True),
    "aside_text": (BlockType.HEADER, True),
    "page_footnote": (BlockType.FOOTNOTE, False),  # 正文脚注：保留
}


def _lines(raw: dict[str, Any]) -> tuple[list[LayoutLine], str | None]:
    """返回块内的行，以及块内独立公式的 LaTeX（如有）。"""
    lines: list[LayoutLine] = []
    latex: str | None = None
    for line in raw.get("lines", []):
        spans: list[LayoutSpan] = []
        for s in line.get("spans", []):
            t = s.get("type")
            content = s.get("content", "")
            if t == "text":
                spans.append(LayoutSpan(kind="text", content=content, bbox=Rect.of(s["bbox"])))
            elif t == "inline_equation":
                spans.append(LayoutSpan(kind="inline_formula", content=content, bbox=Rect.of(s["bbox"])))
            elif t == "interline_equation":
                latex = content
        lines.append(LayoutLine(bbox=Rect.of(line["bbox"]), spans=spans))
    return lines, latex


def _block(raw: dict[str, Any], btype: BlockType, order: int, *, discarded: bool = False) -> LayoutBlock:
    lines, latex = _lines(raw)
    return LayoutBlock(
        type=btype,
        bbox=Rect.of(raw["bbox"]),
        order=order,
        lines=lines,
        latex=latex,
        level=raw.get("level"),
        merge_prev=bool(raw.get("merge_prev", False)),
        placeholder=bool(raw.get("lines_deleted")) and not raw.get("lines"),
        discarded=discarded,
        source_type=raw.get("type", ""),
    )


def normalize(layout: dict[str, Any]) -> LayoutResult:
    pages: list[LayoutPage] = []
    for info in layout.get("pdf_info", []):
        width, height = info["page_size"]
        blocks: list[LayoutBlock] = []
        for raw in sorted(info.get("para_blocks", []), key=lambda b: b.get("index", 0)):
            t = raw.get("type", "")
            children = raw.get("blocks")
            if children:
                for child in sorted(children, key=lambda c: c.get("index", 0)):
                    ctype = _CHILD_TYPES.get(child.get("type", ""), BlockType.FIGURE)
                    blocks.append(_block(child, ctype, len(blocks)))
            else:
                blocks.append(_block(raw, _TOP_TYPES.get(t, BlockType.TEXT), len(blocks)))
        for raw in info.get("discarded_blocks", []):
            btype, discarded = _DISCARDED_TYPES.get(raw.get("type", ""), (BlockType.HEADER, True))
            blocks.append(_block(raw, btype, len(blocks), discarded=discarded))
        pages.append(LayoutPage(index=info.get("page_idx", len(pages)), width=width, height=height, blocks=blocks))
    return LayoutResult(
        parser="mineru",
        parser_version=f"{layout.get('_backend', '')}/{layout.get('_version_name', '')}".strip("/"),
        pages=pages,
    )
