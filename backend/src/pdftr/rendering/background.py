"""去字背景页与公式裁剪图（S5/S5b 验证）。

背景页：只删要翻译的块里的文字，以及完全落在这些块里的线条（行内公式的分数线、根号）；
图片、表格线、独立公式、页眉页码都保留。阅读器在背景页上叠加译文。
"""

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import pymupdf

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockType

SHRINK = 0.5  # 块框略收缩，避免误删相邻的公式字符（S5）
COVER_PAD = 1.0  # 扫描页盖住原文的矩形向外扩一点：解析器的块框紧贴字形，字的边缘会露出来

# PyMuPDF 运行时有这些常量，但类型声明里没有；取值见官方文档 Page.apply_redactions
_IMAGE_NONE = getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0)
_LINE_ART_REMOVE_IF_TOUCHED = getattr(pymupdf, "PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED", 2)
_TEXT_REMOVE = getattr(pymupdf, "PDF_REDACT_TEXT_REMOVE", 0)
_LINE_ART_NONE = getattr(pymupdf, "PDF_REDACT_LINE_ART_NONE", 0)


def _label_band(r: Rect) -> Rect:
    """图内标签只删中间一条窄带：字框按字体上下边界计算，比字形高，图里上下相邻的刻度数字
    （如坐标轴标题正上方的刻度）字框会伸进标签的范围；MuPDF 删除所有字框与删除区域相交的字，会把它们一起删掉。"""
    band = r.height * 0.3
    return Rect(x0=r.x0 + SHRINK, y0=r.y0 + band, x1=r.x1 - SHRINK, y1=r.y1 - band)


def paper_color(page: pymupdf.Page, rect: pymupdf.Rect) -> tuple[float, float, float]:
    """块里纸张的颜色：文字只是少数深色像素，取较亮的 60% 像素的中位数（纸张发黄、发灰也能跟上）。"""
    pix = page.get_pixmap(clip=rect, dpi=36, colorspace=pymupdf.csRGB, alpha=False)
    data, n = pix.samples, pix.n
    pixels = sorted(
        (data[i : i + 3] for i in range(0, len(data) - 2, n)), key=lambda p: p[0] * 299 + p[1] * 587 + p[2] * 114
    )
    if not pixels:
        return (1.0, 1.0, 1.0)
    bright = pixels[len(pixels) * 2 // 5 :]
    r, g, b = bright[len(bright) // 2]
    return (r / 255, g / 255, b / 255)


def make_background(pdf: Path, blocks: Iterable[Block], out: Path, scanned: set[int] | None = None) -> int:
    """返回实际去字的块数。

    图里的文字只删文字、不碰任何线条（坐标轴、刻度、连线紧挨着标签），所以与正文分两轮去字。
    scanned：扫描页（原文在图片里删不掉）：先删掉隐形 OCR 文字，再用纸张颜色的矩形盖住原文。
    """
    scanned = scanned or set()
    by_page: dict[int, list[Rect]] = defaultdict(list)
    labels: dict[int, list[Rect]] = defaultdict(list)
    for b in blocks:
        if b.type is BlockType.FIGURE_TEXT:
            labels[b.page].append(_label_band(b.bbox))
        else:
            by_page[b.page].append(b.bbox.shrink(SHRINK))
    with pymupdf.open(pdf) as doc:
        for page_no in sorted(set(by_page) | set(labels)):
            if page_no >= len(doc):
                continue
            page = doc[page_no]
            covers = []
            if page_no in scanned:  # 先在原图上取好纸色，再改动页面
                for r in by_page[page_no]:
                    box = pymupdf.Rect(r.x0 - COVER_PAD, r.y0 - COVER_PAD, r.x1 + COVER_PAD, r.y1 + COVER_PAD)
                    covers.append((box & page.rect, paper_color(page, box & page.rect)))
            for rects, graphics in ((labels[page_no], _LINE_ART_NONE), (by_page[page_no], _LINE_ART_REMOVE_IF_TOUCHED)):
                if not rects:
                    continue
                for r in rects:
                    page.add_redact_annot(pymupdf.Rect(r.x0, r.y0, r.x1, r.y1))
                page.apply_redactions(images=_IMAGE_NONE, graphics=graphics, text=_TEXT_REMOVE)
            for box, color in covers:
                page.draw_rect(box, color=None, fill=color, overlay=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part")
        doc.save(tmp, garbage=3, deflate=True)
        tmp.replace(out)
    return sum(len(v) for v in by_page.values()) + sum(len(v) for v in labels.values())


def crop_png(pdf: Path, page: int, bbox: Rect, zoom: float = 4.0, pad: float = 1.0) -> bytes:
    """按原文位置裁剪公式图片（透明背景），供 KaTeX 渲染失败时兜底。"""
    with pymupdf.open(pdf) as doc:
        p = doc[page]
        clip = pymupdf.Rect(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad) & p.rect
        pix = p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=True)
        return pix.tobytes("png")
