"""导出 PDF：在去字背景页上排入译文（纯译文），或与原文左右并排（双语对照）。

只用 PyMuPDF，不需要浏览器，内存占用小。译文由 rendering/typesetter.py 按《中文排版需求》排版后直接写入字形；
行内公式用原文位置的裁剪图（矢量重绘需要 KaTeX，服务端不具备）；排不下时先收紧行距，再缩小字号。
字体按 GB/T 7713.2 附录 B：正文宋体、标题黑体、图表编号黑体，西文用 Times 类字体（见 rendering/fonts.py），
与网页阅读器一致；按用到的字形子集嵌入。
"""

import re
from pathlib import Path
from typing import Literal

import pymupdf

from pdftr.domain.ir import Block, BlockType, Document, Segment
from pdftr.rendering.typeset import LINE_HEIGHTS, drop_fake_scripts, wants_indent
from pdftr.rendering.typesetter import FontSet, Token, Typesetter

ExportMode = Literal["mono", "dual"]
Side = Literal["left", "right"]  # 双语对照时译文放在哪一边
LINE_HEIGHT = 1.2  # 最紧的行距（单行框补高按它计算）
MIN_SCALE = 0.4  # 放不下时字号最多缩到原来的 40%

_BREAK_AFTER = set("，。；：、）！？,;:.)")
_SEARCH = 40
# 图表标题开头的编号（“图 4.”“表2：”），按 GB/T 7713.2 用黑体（与前端 entities/document/model.ts 一致）
CAPTION_NO = re.compile(r"^\s*(?:图|表)\s*[A-Z]?\d+(?:\.\d+)*[.:：．。]?")


def _inside_placeholder(text: str, pos: int) -> bool:
    start = text.rfind("<", 0, pos)
    return start != -1 and text.find(">", start) >= pos


def _cut_near(text: str, target: int) -> int:
    for d in range(_SEARCH + 1):
        for pos in (target - d, target + d):
            if 0 < pos < len(text) and text[pos - 1] in _BREAK_AFTER and not _inside_placeholder(text, pos):
                return pos
    pos = min(max(target, 1), len(text) - 1)
    while pos < len(text) and _inside_placeholder(text, pos):
        pos += 1
    return pos


def split_by_parts(text: str, part_chars: list[int]) -> list[str]:
    """把跨栏/跨页段落的译文按各块原文长度拆开，优先在标点处断开（与前端 shared/text/split.ts 一致）。"""
    if len(part_chars) <= 1:
        return [text]
    total = sum(part_chars) or 1
    pieces, start, cumulative = [], 0, 0
    for chars in part_chars[:-1]:
        cumulative += chars
        target = round(len(text) * cumulative / total)
        cut = len(text) if start >= len(text) else _cut_near(text, max(target, start + 1))
        pieces.append(text[start:cut])
        start = cut
    pieces.append(text[start:])
    return pieces


def role(block: Block) -> str:
    """排版角色，决定字体（与前端一致）：heading 黑体、caption 编号黑体其余宋体、body 宋体、
    label（图内文字）黑体居中。"""
    if block.type is BlockType.FIGURE_TEXT:
        return "label"
    if block.type is BlockType.TITLE or block.style.bold:
        return "heading"
    if block.type is BlockType.CAPTION:
        return "caption"
    return "body"


def _box(block: Block) -> pymupdf.Rect:
    """排版框：原文块的范围。解析器给单行文字的框只有字形高，装不下一整行时上下各补一半，
    不再统一往下放宽（那样会压到紧挨着的表格线、图片，如表格标题）。"""
    r = block.bbox
    lack = block.style.font_size * LINE_HEIGHT + 0.5 - r.height
    pad = max(lack, 0) / 2
    return pymupdf.Rect(r.x0, r.y0 - pad, r.x1, r.y1 + pad)


def _formula_images(original: pymupdf.Document, seg: Segment) -> dict[str, tuple[bytes, float, float]]:
    """本段落行内公式的裁剪图：占位符 → (PNG, 宽, 高)，宽高为原文中的尺寸（pt）。"""
    out: dict[str, tuple[bytes, float, float]] = {}
    for key, ref in seg.formula_refs.items():
        if ref.page >= len(original):
            continue
        b = ref.bbox
        clip = pymupdf.Rect(b.x0 - 0.5, b.y0 - 0.5, b.x1 + 0.5, b.y1 + 0.5) & original[ref.page].rect
        pix = original[ref.page].get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=clip, alpha=True)
        out[key] = (pix.tobytes("png"), clip.width, clip.height)
    return out


def _place(
    ts: Typesetter,
    page: pymupdf.Page,
    block: Block,
    tokens: list[Token],
    indent: bool,
    images: dict[str, tuple[bytes, float, float]],
) -> None:
    """按原字号依次尝试 1.5→1.2 倍行距，放得下就用；都放不下时用最紧的行距、整体缩小字号。"""
    box = _box(block)
    size = block.style.font_size
    r = role(block)
    align = {"label": "center", "heading": "left"}.get(r, "justify")
    scale = 1.0
    for line_height in LINE_HEIGHTS:
        lines, bases, height = ts.layout(tokens, box.width, size, line_height, indent)
        if height <= box.height + 0.5:
            break
    else:
        # 在放大的排版框里排版，再整体缩回，相当于缩小字号；二分查找最大的可行比例
        lo, hi = MIN_SCALE, 1.0
        for _ in range(10):
            mid = (lo + hi) / 2
            _, _, height = ts.layout(tokens, box.width / mid, size, LINE_HEIGHT, indent)
            lo, hi = (mid, hi) if height * mid <= box.height + 0.5 else (lo, mid)
        scale = lo
        lines, bases, _ = ts.layout(tokens, box.width / scale, size, LINE_HEIGHT, indent)
    c = block.style.color
    writer = pymupdf.TextWriter(page.rect)
    ts.draw(page, writer, (box.x0, box.y0), box.width / scale, lines, bases, size, align, indent,
            lambda key: images[key][0] if key in images else None, scale)  # fmt: skip
    writer.write_text(page, color=((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255))


def _typeset_all(src: pymupdf.Document, out: pymupdf.Document, doc: Document, segments: list[Segment],
                 font_dir: Path | None) -> None:  # fmt: skip
    """把全部译文排进去字背景页。未翻译或失败的段落排入原文，避免出现空白。"""
    ts = Typesetter(FontSet.load(font_dir))
    blocks = {b.id: b for b in doc.blocks()}
    for seg in segments:
        images = _formula_images(src, seg)
        texts = split_by_parts(seg.target or seg.source, [p.chars for p in seg.parts] or [len(seg.source)])
        for n, (part, piece) in enumerate(zip(seg.parts, texts, strict=False)):
            block = blocks.get(part.block_id)
            if block is None or not piece.strip() or block.page >= len(out):
                continue
            r = role(block)
            caption_no = ""
            if r == "caption" and n == 0 and seg.target and (m := CAPTION_NO.match(piece)):
                caption_no = m.group(0)
            size = lambda key, imgs=images: imgs[key][1:] if key in imgs else None  # noqa: E731
            tokens = ts.tokenize(drop_fake_scripts(piece.strip()), r, block.style.font_size, size, caption_no)
            indent = bool(seg.target) and wants_indent(block, seg, first=n == 0)
            _place(ts, out[block.page], block, tokens, indent, images)


def export_pdf(
    original: Path,
    background: Path,
    doc: Document,
    segments: list[Segment],
    mode: ExportMode,
    font_dir: Path | None = None,
    side: Side = "right",
) -> bytes:
    """返回导出的 PDF 字节。font_dir 缺字体时用内置字体。"""
    with pymupdf.open(original) as src, pymupdf.open(background) as translated:
        _typeset_all(src, translated, doc, segments, font_dir)
        translated.subset_fonts()  # 中文字体只保留用到的字形
        if mode == "mono":
            return translated.tobytes(garbage=3, deflate=True)

        out = pymupdf.open()
        for i in range(len(src)):
            w, h = src[i].rect.width, src[i].rect.height
            page = out.new_page(width=w * 2, height=h)
            left, right = (translated, src) if side == "left" else (src, translated)
            page.show_pdf_page(pymupdf.Rect(0, 0, w, h), left, i)
            page.show_pdf_page(pymupdf.Rect(w, 0, w * 2, h), right, i)
        data = out.tobytes(garbage=3, deflate=True)
        out.close()
        return data
