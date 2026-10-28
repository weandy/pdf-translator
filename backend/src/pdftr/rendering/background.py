"""去字背景页与公式裁剪图（S5/S5b 验证）。

背景页：只删要翻译的块里的文字，以及完全落在这些块里的线条（行内公式的分数线、根号）；
图片、表格线、独立公式、页眉页码都保留。阅读器在背景页上叠加译文。
"""

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import pymupdf

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block

SHRINK = 0.5  # 块框略收缩，避免误删相邻的公式字符（S5）

# PyMuPDF 运行时有这些常量，但类型声明里没有；取值见官方文档 Page.apply_redactions
_IMAGE_NONE = getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0)
_LINE_ART_REMOVE_IF_TOUCHED = getattr(pymupdf, "PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED", 2)
_TEXT_REMOVE = getattr(pymupdf, "PDF_REDACT_TEXT_REMOVE", 0)


def make_background(pdf: Path, blocks: Iterable[Block], out: Path) -> int:
    """返回实际去字的块数。"""
    by_page: dict[int, list[Rect]] = defaultdict(list)
    for b in blocks:
        by_page[b.page].append(b.bbox.shrink(SHRINK))
    with pymupdf.open(pdf) as doc:
        for page_no, rects in by_page.items():
            if page_no >= len(doc):
                continue
            page = doc[page_no]
            for r in rects:
                page.add_redact_annot(pymupdf.Rect(r.x0, r.y0, r.x1, r.y1))
            page.apply_redactions(images=_IMAGE_NONE, graphics=_LINE_ART_REMOVE_IF_TOUCHED, text=_TEXT_REMOVE)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part")
        doc.save(tmp, garbage=3, deflate=True)
        tmp.replace(out)
    return sum(len(v) for v in by_page.values())


def crop_png(pdf: Path, page: int, bbox: Rect, zoom: float = 4.0, pad: float = 1.0) -> bytes:
    """按原文位置裁剪公式图片（透明背景），供 KaTeX 渲染失败时兜底。"""
    with pymupdf.open(pdf) as doc:
        p = doc[page]
        clip = pymupdf.Rect(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad) & p.rect
        pix = p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=True)
        return pix.tobytes("png")
