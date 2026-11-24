"""去字背景页与公式裁剪图（S5/S5b 验证）。

背景页：只删要翻译的块里的文字，以及完全落在这些块里的线条（行内公式的分数线、根号）；
图片、表格线、独立公式、页眉页码都保留。阅读器在背景页上叠加译文。
"""

from collections import defaultdict
from collections.abc import Iterable
from itertools import chain
from pathlib import Path
from statistics import median

import pymupdf

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockType
from pdftr.rendering.artifacts import pdf_destination

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


def _glyph_rows(page: pymupdf.Page, clip: pymupdf.Rect | None = None) -> Iterable[list[Rect]]:
    text = page.get_textpage(clip=clip, flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    for block in text.extractRAWDICT()["blocks"]:
        for line in block.get("lines", []):
            boxes = [Rect.of(ch["bbox"]) for span in line["spans"] for ch in span["chars"] if ch["c"].strip()]
            if boxes:
                if abs(line["dir"][1]) < 0.01:
                    yield boxes
                else:
                    yield from ([box] for box in boxes)


def _safe_body_rects(page: pymupdf.Page, rects: list[Rect]) -> list[Rect]:
    # 从每个待擦框减掉外部字框的交集。上下左右分别切开，保留同一行其余位置的擦除面积。
    pieces = [[r] for r in rects]
    guards = []
    for row in _glyph_rows(page):
        # 紧邻行的下标中心有时也伸进了下一正文块，归属时同时核对原生行的位置。
        row_y = median(box.center[1] for box in row)
        for glyph in row:
            x, y = glyph.center
            if not any(r.y0 <= row_y <= r.y1 and r.contains_point(x, y) for r in rects):
                guards.append(glyph)
    for glyph in guards:
        # 极窄的字形和零宽组合符也要避开，且浮点坐标写回 PDF 后仍要分离。
        guard = glyph.shrink(-0.01)
        for i, r in enumerate(rects):
            if not r.intersection(guard):
                continue
            kept = []
            for piece in pieces[i]:
                cut = piece.intersection(guard)
                if cut is None:
                    kept.append(piece)
                    continue
                for part in (
                    (piece.x0, piece.y0, piece.x1, cut.y0),
                    (piece.x0, cut.y1, piece.x1, piece.y1),
                    (piece.x0, cut.y0, cut.x0, cut.y1),
                    (cut.x1, cut.y0, piece.x1, cut.y1),
                ):
                    if part[0] < part[2] and part[1] < part[3]:
                        kept.append(Rect.of(part))
            pieces[i] = kept
    return [piece for group in pieces for piece in group]


def make_background(pdf: Path, blocks: Iterable[Block], out: Path, scanned: set[int] | None = None) -> int:
    """返回实际去字的块数。

    图里的文字只删文字、不碰任何线条（坐标轴、刻度、连线紧挨着标签），所以与正文分两轮去字。
    scanned：扫描页（原文在图片里删不掉）：先删掉隐形 OCR 文字，再用纸张颜色的矩形盖住原文。
    """
    scanned = scanned or set()
    by_page: dict[int, list[Rect]] = defaultdict(list)
    overflows: dict[int, list[Rect]] = defaultdict(list)
    labels: dict[int, list[Rect]] = defaultdict(list)
    for b in blocks:
        if b.type is BlockType.FIGURE_TEXT:
            labels[b.page].append(_label_band(b.bbox))
        else:
            by_page[b.page].append(b.bbox.shrink(SHRINK))
            for span in b.spans:
                if span.bbox.x1 > b.bbox.x1 and span.bbox.height > 0:
                    overflows[b.page].append(
                        Rect(x0=b.bbox.x1 - 1.0, y0=span.bbox.y0, x1=span.bbox.x1 + 0.5, y1=span.bbox.y1)
                    )
    with pdf_destination(out) as tmp, pymupdf.open(pdf) as doc:
        for page_no in sorted(set(by_page) | set(labels)):
            if page_no >= len(doc):
                continue
            page = doc[page_no]
            covers = []
            if page_no in scanned:  # 先在原图上取好纸色，再改动页面
                for r in by_page[page_no]:
                    box = pymupdf.Rect(r.x0 - COVER_PAD, r.y0 - COVER_PAD, r.x1 + COVER_PAD, r.y1 + COVER_PAD)
                    covers.append((box & page.rect, paper_color(page, box & page.rect)))
            body_rects = [*by_page[page_no], *overflows[page_no]]
            if body_rects and page_no not in scanned:
                body_rects = _safe_body_rects(page, body_rects)
            for rects, graphics in ((labels[page_no], _LINE_ART_NONE), (body_rects, _LINE_ART_REMOVE_IF_TOUCHED)):
                if not rects:
                    continue
                for r in rects:
                    clip = pymupdf.Rect(r.x0, r.y0, r.x1, r.y1) & page.rect
                    if not clip.is_empty:
                        page.add_redact_annot(clip)
                page.apply_redactions(images=_IMAGE_NONE, graphics=graphics, text=_TEXT_REMOVE)
            for box, color in covers:
                page.draw_rect(box, color=None, fill=color, overlay=True)
        doc.save(tmp, garbage=3, deflate=True)
    return sum(len(v) for v in by_page.values()) + sum(len(v) for v in labels.values())


def _trim_formula_edges(pix: pymupdf.Pixmap, zoom: float) -> pymupdf.Pixmap:
    """按垂直墨迹连通带清除贴边且与主体隔开一段空白的邻行残影，保留公式底边的基线参照。"""
    data, stride, height = pix.samples, pix.stride, pix.height
    if height <= 2:
        return pix

    def row_ink(y: int) -> int:
        row = data[y * stride : (y + 1) * stride]
        # RGBA 为预乘透明度的颜色，透明与接近白色的像素不算墨迹。
        return sum(
            1
            for r, g, b, a in zip(row[0::4], row[1::4], row[2::4], row[3::4], strict=True)
            if a >= 32 and a - min(r, g, b) >= 24
        )

    counts = [row_ink(y) for y in range(height)]
    merge_gap = max(2, round(0.5 * zoom))
    runs: list[tuple[int, int, int]] = []  # (y0, y1_exclusive, ink_pixels)
    y = 0
    while y < height:
        if not counts[y]:
            y += 1
            continue
        start = y
        ink_sum = 0
        while y < height and counts[y]:
            ink_sum += counts[y]
            y += 1
        if runs and start - runs[-1][1] < merge_gap:
            prev_y0, _, prev_ink = runs[-1]
            runs[-1] = (prev_y0, y, prev_ink + ink_sum)
        else:
            runs.append((start, y, ink_sum))

    if len(runs) < 2:
        return pix

    main_idx = max(range(len(runs)), key=lambda i: (runs[i][1] - runs[i][0], runs[i][2]))
    main_h = runs[main_idx][1] - runs[main_idx][0]
    # 合法数学符号笔画（≤/≥ 底横线、\tilde/\bar 重音）内部空白在 4x 下 <= 5px，跨行残影缝隙 >= 6px。
    min_gap = max(5, round(1.4 * zoom))
    strong_gap = max(6, round(1.5 * zoom))
    narrow_band = min(int(2.0 * zoom), height // 4)
    thin_bar_h = max(3, round(1.0 * zoom))

    def is_fraction_like(edge_idx: int, neighbor_idx: int) -> bool:
        if len(runs) < 3:
            return False
        nb_y0, nb_y1, _ = runs[neighbor_idx]
        if nb_y1 - nb_y0 > thin_bar_h:
            return False
        other_idx = neighbor_idx + (1 if neighbor_idx > edge_idx else -1)
        if not 0 <= other_idx < len(runs):
            return False
        edge_h = runs[edge_idx][1] - runs[edge_idx][0]
        other_h = runs[other_idx][1] - runs[other_idx][0]
        return min(edge_h, other_h) >= 0.4 * max(edge_h, other_h)

    def should_trim_edge(edge_idx: int, *, is_top: bool) -> bool:
        y0, y1, _ = runs[edge_idx]
        edge_h = y1 - y0
        if is_top:
            if y0 > 1 or main_idx == 0:
                return False
            sep = runs[1][0] - y1
            touches_border = y0 == 0
            if is_fraction_like(0, 1):
                return False
        else:
            if y1 < height - 1 or main_idx == len(runs) - 1:
                return False
            sep = y0 - runs[-2][1]
            touches_border = y1 == height
            if is_fraction_like(len(runs) - 1, len(runs) - 2):
                return False
        if narrow_band and edge_h <= narrow_band and sep >= min_gap and touches_border:
            return True
        return (
            sep >= min_gap
            and edge_h < 0.7 * main_h
            and edge_h <= int(0.42 * height)
            and (touches_border or sep >= strong_gap)
        )

    top = runs[0][1] if should_trim_edge(0, is_top=True) else 0
    bottom = height - runs[-1][0] if should_trim_edge(len(runs) - 1, is_top=False) else 0
    if not (top or bottom) or top + bottom >= height or not any(counts[top : height - bottom]):
        return pix
    # 排版以图片底边对齐基线，底部残影行只清透明度并保留高度。
    kept = data[top * stride : (height - bottom) * stride]
    return pymupdf.Pixmap(pymupdf.csRGB, pix.width, height - top, kept + bytes(bottom * stride), True)


def crop_formula(page: pymupdf.Page, bbox: Rect, zoom: float = 4.0) -> tuple[bytes, float, float] | None:
    """裁图及其实际宽高（pt），给边缘抗锯齿留水平 0.3pt、垂直 0.25pt。"""
    clip = pymupdf.Rect(bbox.x0 - 0.3, bbox.y0 - 0.25, bbox.x1 + 0.3, bbox.y1 + 0.25) & page.rect
    if clip.is_empty:
        return None
    # 只需检查同一行附近的字，查询框多留出字高，确保紧邻的完整字框可提取。
    margin = max(16.0, bbox.height)
    search = pymupdf.Rect(clip.x0 - margin, clip.y0 - margin, clip.x1 + margin, clip.y1 + margin)
    for ch in chain.from_iterable(_glyph_rows(page, search)):
        if ch.y0 >= bbox.y1 or ch.y1 <= bbox.y0:
            continue
        x, _ = ch.center
        if x < bbox.x0:
            clip.x0 = max(clip.x0, min(ch.x1, bbox.x0))
        elif x > bbox.x1:
            clip.x1 = min(clip.x1, max(ch.x0, bbox.x1))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, colorspace=pymupdf.csRGB, alpha=True)
    pix = _trim_formula_edges(pix, zoom)
    return pix.tobytes("png"), pix.width / zoom, pix.height / zoom


def crop_png(pdf: Path, page: int, bbox: Rect, zoom: float = 4.0) -> bytes:
    """按原文位置裁剪公式图片（透明背景），供 KaTeX 渲染失败时兜底。"""
    with pymupdf.open(pdf) as doc:
        image = crop_formula(doc[page], bbox, zoom)
    return image[0] if image else b""
