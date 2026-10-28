"""导出 PDF：在去字背景页上排入译文（纯译文），或与原文左右并排（双语对照）。

只用 PyMuPDF（insert_htmlbox），不需要浏览器，内存占用小。
行内公式用原文位置的裁剪图（矢量重绘需要 KaTeX，服务端不具备）；排不下时自动缩小字号。
中文字体由 MuPDF 内置的后备字体提供（Droid Sans Fallback），按用到的字形子集嵌入。
"""

import html
import io
import re
from pathlib import Path
from typing import Literal

import pymupdf

from pdftr.domain.ir import Block, Document, Segment

ExportMode = Literal["mono", "dual"]

_BREAK_AFTER = set("，。；：、）！？,;:.)")
_SEARCH = 40
_PLACEHOLDER = re.compile(r"<(m\d+)/>")


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


def _to_html(text: str, seg: Segment, images: dict[str, tuple[str, float, float]]) -> str:
    """译文 → HTML：转义全部文字，只放行上下标等白名单标签，公式占位符换成裁剪图。"""
    out = []
    for i, piece in enumerate(_PLACEHOLDER.split(text)):
        if i % 2:  # 占位符
            img = images.get(f"{seg.id}:{piece}")
            if img:
                name, w, h = img
                out.append(f'<img src="{name}" style="width:{w:.1f}pt;height:{h:.1f}pt;vertical-align:middle"/>')
            continue
        escaped = html.escape(piece, quote=False)
        out.append(re.sub(r"&lt;(/?)(sup|sub|b|i)&gt;", r"<\1\2>", escaped))
    return "".join(out)


def _css(block: Block) -> str:
    color = f"#{block.style.color:06x}"
    weight = "bold" if block.style.bold else "normal"
    return (
        f"* {{font-family: sans-serif; font-size: {block.style.font_size:.1f}pt; line-height: 1.35;"
        f" color: {color}; font-weight: {weight}; margin: 0; padding: 0; text-align: justify}}"
    )


def _formula_archive(original: pymupdf.Document, seg: Segment) -> tuple[pymupdf.Archive | None, dict]:
    """本段落的公式裁剪图。每个段落单独打包：共用一个大包时每次排版都会处理全部图片，又慢又占内存。"""
    if not seg.formula_refs:
        return None, {}
    archive = pymupdf.Archive()
    images: dict[str, tuple[str, float, float]] = {}
    for key, ref in seg.formula_refs.items():
        if ref.page >= len(original):
            continue
        b = ref.bbox
        clip = pymupdf.Rect(b.x0 - 0.5, b.y0 - 0.5, b.x1 + 0.5, b.y1 + 0.5) & original[ref.page].rect
        pix = original[ref.page].get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=clip, alpha=True)
        archive.add(pix.tobytes("png"), f"{key}.png")
        images[f"{seg.id}:{key}"] = (f"{key}.png", clip.width, clip.height)
    return archive, images


def _overlay(src: pymupdf.Document, doc: Document, segments: list[Segment]) -> pymupdf.Document:
    """把全部译文排进一份与原文同尺寸的透明叠加 PDF。

    每页共用一个写入器：PyMuPDF 的 insert_htmlbox 每次调用都会生成一份带完整中文字体的临时 PDF，
    17 页论文就会嵌入几百份字体，内存超过 1 GB；这里同一份文档里字体只嵌入一次。
    """
    blocks = {b.id: b for b in doc.blocks()}
    boxes: dict[int, list[tuple[Block, str, pymupdf.Archive | None]]] = {}
    for seg in segments:
        archive, images = _formula_archive(src, seg)
        texts = split_by_parts(seg.target or seg.source, [p.chars for p in seg.parts] or [len(seg.source)])
        for part, piece in zip(seg.parts, texts, strict=False):
            block = blocks.get(part.block_id)
            if block is not None and piece.strip() and block.page < len(src):
                boxes.setdefault(block.page, []).append((block, _to_html(piece, seg, images), archive))

    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    for i in range(len(src)):
        dev = writer.begin_page(src[i].rect)
        for block, body, archive in boxes.get(i, []):
            r = block.bbox
            target = pymupdf.Rect(r.x0, r.y0, r.x1, r.y1 + block.style.font_size * 0.6)
            story = pymupdf.Story(html=body, user_css=_css(block), archive=archive)
            # 排不下时把排版框按比例放大，再整体缩回目标框，相当于缩小字号
            fit = story.fit_scale(pymupdf.Rect(0, 0, target.width, target.height), scale_min=1, scale_max=None)
            if fit is None or not fit.parameter:
                continue
            scale = 1 / fit.parameter
            story.reset()
            story.place(fit.rect)
            story.draw(dev, pymupdf.Matrix(scale, scale).pretranslate(0, 0) * pymupdf.Matrix(1, 0, 0, 1, r.x0, r.y0))
        writer.end_page()
    writer.close()
    return pymupdf.open("pdf", buf.getvalue())


def export_pdf(original: Path, background: Path, doc: Document, segments: list[Segment], mode: ExportMode) -> bytes:
    """返回导出的 PDF 字节。未翻译或失败的段落排入原文，避免出现空白。"""
    with pymupdf.open(original) as src, pymupdf.open(background) as translated, _overlay(src, doc, segments) as ov:
        for i in range(len(translated)):
            translated[i].show_pdf_page(translated[i].rect, ov, i)
        translated.subset_fonts()  # 中文字体只保留用到的字形，体积从数 MB 降到几百 KB
        if mode == "mono":
            return translated.tobytes(garbage=3, deflate=True)

        out = pymupdf.open()
        for i in range(len(src)):
            w, h = src[i].rect.width, src[i].rect.height
            page = out.new_page(width=w * 2, height=h)
            page.show_pdf_page(pymupdf.Rect(0, 0, w, h), src, i)
            page.show_pdf_page(pymupdf.Rect(w, 0, w * 2, h), translated, i)
        data = out.tobytes(garbage=3, deflate=True)
        out.close()
        return data
