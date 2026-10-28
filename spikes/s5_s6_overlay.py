"""验证实验 S5 + S6：去字背景页 + 浏览器叠加层导出 + 合成。

用 PyMuPDF 自带的文字块代替版面解析器，用伪译文代替 LLM，
只验证版式链路：去字是否干净、叠加层是否矢量、坐标是否对齐。

用法：python s5_s6_overlay.py samples/1706.03762.pdf 0 1 2
"""

import base64
import html
import json
import re
import sys
import time
from pathlib import Path

import pymupdf
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "out"

# 数学字体特征：TeX 的 CM*/MS*、各类 Math/Symbol 字体
MATH_FONT = re.compile(r"CM(MI|SY|EX|BSY|MIB)|MSBM|MSAM|Math|Symbol|STIX|Cambria|rsfs|eufm", re.I)

# 伪译文素材：按"英文字符数 × 系数"截取，模拟英译中后的长度
ZH_SAMPLE = (
    "本文提出了一种新的网络结构，完全基于注意力机制，摒弃了循环和卷积。"
    "在两个机器翻译任务上的实验表明，该模型在质量上更优，同时更易于并行化，"
    "训练所需时间显著减少。我们进一步证明该方法可以很好地推广到其他任务。"
)
ZH_RATIO = 0.4


def pseudo_translate(text: str) -> str:
    n = max(2, round(len(text) * ZH_RATIO))
    return (ZH_SAMPLE * (n // len(ZH_SAMPLE) + 1))[:n]


def is_math_span(span) -> bool:
    return bool(MATH_FONT.search(span["font"]))


def collect_blocks(page):
    """把 PyMuPDF 文字块分成可翻译块和公式块。"""
    blocks = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        spans = [s for line in b["lines"] for s in line["spans"] if s["text"].strip()]
        if not spans:
            continue
        math_chars = sum(len(s["text"]) for s in spans if is_math_span(s))
        total = sum(len(s["text"]) for s in spans)
        text = " ".join(s["text"] for s in spans)
        words = len(re.findall(r"[A-Za-z]{2,}", text))
        sizes = sorted(s["size"] for s in spans)
        blocks.append({
            "bbox": b["bbox"],
            "spans": spans,
            "text": text,
            "size": sizes[len(sizes) // 2],
            "bold": any("Bold" in s["font"] or s["flags"] & 16 for s in spans),
            "translatable": math_chars / total < 0.4 and words >= 2,
        })
    return blocks


def crop_data_uri(page, rect, zoom=4) -> str:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect, alpha=True)
    return "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()


def block_html(page, blk) -> str:
    """把块渲染成 HTML：普通文字换成伪译文，行内数学字形换成原图裁剪。"""
    parts, buf = [], []
    for s in blk["spans"]:
        if is_math_span(s):
            if buf:
                parts.append(html.escape(pseudo_translate(" ".join(buf))))
                buf = []
            r = pymupdf.Rect(s["bbox"])
            h = r.height
            parts.append(
                f'<img src="{crop_data_uri(page, r)}" '
                f'style="height:{h / blk["size"]:.2f}em;vertical-align:-0.2em">'
            )
        else:
            buf.append(s["text"])
    if buf:
        parts.append(html.escape(pseudo_translate(" ".join(buf))))
    return "".join(parts)


def make_background(doc, page_blocks):
    """S5：只删可翻译块里的文字，图片和矢量图形不动。"""
    for pno, blocks in page_blocks.items():
        page = doc[pno]
        for blk in blocks:
            if blk["translatable"]:
                # 略收缩，避免误删相邻公式字符
                r = pymupdf.Rect(blk["bbox"]) + (0.5, 0.5, -0.5, -0.5)
                page.add_redact_annot(r)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            # 完全落在文字块内的线条（行内公式的分数线、根号）一并删除；
            # 表格线、图表不在文字块内，不受影响
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )


FIT_JS = """
() => {
  // 先调行距再二分字号，下限为原字号的 70%
  const res = [];
  for (const el of document.querySelectorAll('.blk')) {
    const base = parseFloat(el.dataset.size), H = el.clientHeight;
    const inner = el.firstElementChild;
    const fits = () => inner.scrollHeight <= H + 0.5;
    let lh = 1.3, size = base;
    el.style.fontSize = size + 'pt';
    while (!fits() && lh > 1.05) { lh -= 0.05; el.style.lineHeight = lh; }
    if (!fits()) {
      let lo = base * 0.7, hi = base;
      for (let i = 0; i < 8; i++) {
        const mid = (lo + hi) / 2; el.style.fontSize = mid + 'pt';
        if (fits()) lo = mid; else hi = mid;
      }
      size = lo; el.style.fontSize = size + 'pt';
    }
    res.push({scale: size / base, overflow: !fits()});
  }
  return res;
}
"""


def overlay_html(page, blocks) -> str:
    w, h = page.rect.width, page.rect.height
    divs = []
    for blk in blocks:
        if not blk["translatable"]:
            continue
        x0, y0, x1, y1 = blk["bbox"]
        weight = "bold" if blk["bold"] else "normal"
        divs.append(
            f'<div class="blk" data-size="{blk["size"]:.2f}" style="left:{x0:.2f}pt;top:{y0:.2f}pt;'
            f'width:{x1 - x0:.2f}pt;height:{y1 - y0:.2f}pt;font-weight:{weight}">'
            f"<div>{block_html(page, blk)}</div></div>"
        )
    return f"""<section class="page" style="width:{w}pt;height:{h}pt">{''.join(divs)}</section>"""


def main(pdf_path: str, pages: list[int]):
    OUT.mkdir(exist_ok=True)
    stem = Path(pdf_path).stem
    src = pymupdf.open(pdf_path)
    page_blocks = {p: collect_blocks(src[p]) for p in pages}

    # 叠加层：每个 PDF 页一个 section，@page 尺寸按第一页（arXiv 样本各页同尺寸）
    w, h = src[pages[0]].rect.width, src[pages[0]].rect.height
    body = "".join(overlay_html(src[p], page_blocks[p]) for p in pages)
    doc_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: {w}pt {h}pt; margin: 0 }}
html, body {{ margin: 0; background: transparent }}
.page {{ position: relative; overflow: hidden; break-after: page }}
.blk {{ position: absolute; font-family: 'WenQuanYi Zen Hei', sans-serif; line-height: 1.3; color: #000 }}
.blk > div {{ text-align: justify }}
</style></head><body>{body}</body></html>"""
    (OUT / f"{stem}.overlay.html").write_text(doc_html, encoding="utf-8")

    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.set_content(doc_html, wait_until="load")
        fit = pg.evaluate(FIT_JS)
        pg.pdf(path=str(OUT / f"{stem}.overlay.pdf"), prefer_css_page_size=True, print_background=False)
        browser.close()
    t_print = time.time() - t0

    # S5：背景页
    bg = pymupdf.open(pdf_path)
    bg.select(pages)
    make_background(bg, {i: page_blocks[p] for i, p in enumerate(pages)})
    bg.save(OUT / f"{stem}.background.pdf", garbage=3, deflate=True)

    # S6：合成
    overlay = pymupdf.open(OUT / f"{stem}.overlay.pdf")
    for i in range(len(bg)):
        bg[i].show_pdf_page(bg[i].rect, overlay, i)
    out_pdf = OUT / f"{stem}.translated.pdf"
    bg.save(out_pdf, garbage=3, deflate=True)

    # 渲染对比图
    orig = pymupdf.open(pdf_path)
    res = pymupdf.open(out_pdf)
    for i, p in enumerate(pages):
        orig[p].get_pixmap(dpi=80).save(OUT / f"{stem}.p{p}.orig.png")
        pymupdf.open(OUT / f"{stem}.background.pdf")[i].get_pixmap(dpi=80).save(OUT / f"{stem}.p{p}.bg.png")
        res[i].get_pixmap(dpi=80).save(OUT / f"{stem}.p{p}.out.png")

    stats = {
        "pages": pages,
        "blocks": sum(len(v) for v in page_blocks.values()),
        "translatable": sum(b["translatable"] for v in page_blocks.values() for b in v),
        "fit_min_scale": round(min((f["scale"] for f in fit), default=1), 3),
        "overflow_blocks": sum(f["overflow"] for f in fit),
        "chromium_seconds": round(t_print, 2),
        "overlay_kb": (OUT / f"{stem}.overlay.pdf").stat().st_size // 1024,
        "output_kb": out_pdf.stat().st_size // 1024,
        "source_kb": Path(pdf_path).stat().st_size // 1024,
        # 输出里能提取到中文 → 叠加层是矢量文字而不是图片
        "vector_text_chars": len(re.findall(r"[一-鿿]", res[0].get_text())),
        "fonts": sorted({f[3] for f in res[0].get_fonts(full=True)}),
    }
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], [int(x) for x in sys.argv[2:]] or [0])
