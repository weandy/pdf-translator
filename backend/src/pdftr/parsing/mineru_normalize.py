"""MinerU layout.json → LayoutResult。

依据 S2/S4 对 5 篇论文的实测：
- 坐标为 PDF point（与 page_size 一致），直接使用。
- 图、表、图表、代码块带子块（body / caption / footnote），展开成独立的块。
- 跨栏/跨页段落：续行被并进前一块，原位置留下 lines 为空、lines_deleted 为真的占位块。
- discarded_blocks 里的页眉、页码、侧边标识不展示；page_footnote 是正文脚注，要保留并翻译。
"""

import math
import re
import sys
from collections.abc import Sequence
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

# arXiv/IEEE 类论文常把章节标题排成 small-caps，MinerU 有时会将其返回为
# 普通 text。标题识别只处理带明确编号、短而无句末标点的块，避免把普通正文
# 或编号列表误判成章节标题。span 之间和相邻行之间统一补空格，兼容 MinerU
# 因字体/字号变化把一个标题拆成多个 span 的情况。
_SECTION_PREFIX = r"(?:[IVXLCDM]+|[A-Z]|\d+(?:\.\d+)*)"
_ENGLISH_SECTION_TITLE = re.compile(rf"^\s*{_SECTION_PREFIX}\s*[.)]?\s+[A-Za-z][A-Za-z0-9]*[A-Za-z0-9\s&'’(),:/-]*\s*$")
_CHINESE_SECTION_TITLE = re.compile(r"^\s*(?:第\s*\d+\s*章|[一二三四五六七八九十百]+[、.])\s*[^。！？!?；;]+\s*$")
_SECTION_END = set("。！？!?；;:")


def _looks_like_section_title(raw: dict[str, Any]) -> bool:
    lines = raw.get("lines", [])
    if not 1 <= len(lines) <= 2:
        return False
    line_texts = []
    for line in lines:
        parts = [str(span.get("content", "")).strip() for span in line.get("spans", []) if span.get("type") == "text"]
        if parts:
            line_texts.append(" ".join(parts))
    text = re.sub(r"\s+", " ", " ".join(line_texts)).strip()
    if not 4 <= len(text) <= 120 or not text or text[-1] in _SECTION_END:
        return False
    words = text.split()
    if len(words) > 14:
        return False
    if len(text) >= 4 and _CHINESE_SECTION_TITLE.fullmatch(text):
        return True
    if len(text) < 8:
        return False
    if not _ENGLISH_SECTION_TITLE.fullmatch(text):
        return False
    # 编号后首个单词必须像标题词（大写或 small-caps），避免匹配普通句子。
    body = re.sub(rf"^\s*{_SECTION_PREFIX}\s*[.)]?\s+", "", text)
    return bool(body and body[0].isupper())


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


def normalize_checked(
    layout: dict[str, Any], *, orig_pages: Sequence[int] | None = None, strict_relative: bool = False
) -> LayoutResult:
    infos = layout.get("pdf_info")
    if not isinstance(infos, list) or not infos:
        raise ValueError("MinerU 解析结果缺少有效页面")
    if strict_relative and not orig_pages:
        raise ValueError("MinerU 分片缺少原始页号映射")
    seen: set[int] = set()
    for info in infos:
        if not isinstance(info, dict):
            raise ValueError("MinerU 解析结果含非法页面")
        index = info.get("page_idx")
        if (
            type(index) is not int
            or index < 0
            or index in seen
            or (orig_pages is not None and index >= len(orig_pages))
        ):
            raise ValueError("MinerU 解析结果含缺失、非法、重复或越界页号")
        size = info.get("page_size")
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or any(
                type(value) not in (int, float) or not 0 < value <= sys.float_info.max or not math.isfinite(value)
                for value in size
            )
        ):
            raise ValueError("MinerU 解析结果含非法页面尺寸")
        seen.add(index)
    result = normalize(layout)
    if strict_relative and orig_pages is not None:
        for page in result.pages:
            page.index = orig_pages[page.index]
    result.pages.sort(key=lambda page: page.index)
    return result


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
                btype = _TOP_TYPES.get(t, BlockType.TEXT)
                if btype is BlockType.TEXT and _looks_like_section_title(raw):
                    btype = BlockType.TITLE
                blocks.append(_block(raw, btype, len(blocks)))
        for raw in info.get("discarded_blocks", []):
            btype, discarded = _DISCARDED_TYPES.get(raw.get("type", ""), (BlockType.HEADER, True))
            blocks.append(_block(raw, btype, len(blocks), discarded=discarded))
        pages.append(LayoutPage(index=info.get("page_idx", len(pages)), width=width, height=height, blocks=blocks))
    return LayoutResult(
        parser="mineru",
        parser_version=f"{layout.get('_backend', '')}/{layout.get('_version_name', '')}".strip("/"),
        pages=pages,
    )
