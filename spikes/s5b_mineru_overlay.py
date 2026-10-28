"""验证实验 S5b：用 MinerU 版面 + 真实译文重做去字背景页与叠加导出。

与 s5_s6_overlay.py 的区别：
- 块的划分、类型来自 MinerU layout.json（不再靠字体猜公式）
- 译文来自 s7_cost.py 的真实 DeepSeek 结果（_cost.json）
- 行内公式用 MinerU 给出的 LaTeX 经 KaTeX 渲染；独立公式、算法框、图表保留原样
- 跨页合并的段落按原文行数比例把译文拆回两页

用法：python s5b_mineru_overlay.py samples/1412.6980.pdf out/mineru/1412.6980/vlm 1 2
"""

import html
import json
import re
import sys
from pathlib import Path

import pymupdf
from playwright.sync_api import sync_playwright

from s7_cost import block_text, segments
from s5_s6_overlay import FIT_JS

OUT = Path(__file__).parent / "out"
TRANSLATE_TYPES = {"text", "title", "image_caption", "table_caption", "chart_caption", "page_footnote"}
KATEX = "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist"


def split_at_ratio(text: str, ratio: float) -> tuple[str, str]:
    """按比例拆分译文，优先在标点处断开，且不拆开占位符。"""
    target = round(len(text) * ratio)
    best = target
    for d in range(0, 40):
        for pos in (target - d, target + d):
            if 0 < pos < len(text) and text[pos - 1] in "，。；：、）,;" and not re.search(r"<m\d*$", text[:pos]):
                return text[:pos], text[pos:]
    return text[:best], text[best:]


def render_inline(text: str, formulas: list[str]) -> str:
    """译文 HTML：<mN/> 换成待 KaTeX 渲染的元素。"""
    out, pos = [], 0
    for m in re.finditer(r"<m(\d+)/>", text):
        out.append(html.escape(text[pos:m.start()]))
        k = int(m.group(1)) - 1
        tex = formulas[k] if k < len(formulas) else ""
        out.append(f'<span class="tex">{html.escape(tex)}</span>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def block_style(page, bbox):
    """从 PyMuPDF 取块内主字号和是否粗体。"""
    sizes, bold = [], 0
    for b in page.get_text("dict", clip=pymupdf.Rect(bbox))["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                if s["text"].strip():
                    sizes += [s["size"]] * len(s["text"])
                    bold += len(s["text"]) if (s["flags"] & 16 or "Bold" in s["font"] or "Medi" in s["font"]) else 0
    sizes.sort()
    size = sizes[len(sizes) // 2] if sizes else 10
    return size, bold > len(sizes) / 2


def main(pdf_path: str, mineru_dir: str, *page_args):
    pages = [int(p) for p in page_args] or [1]
    md = Path(mineru_dir)
    layout = json.loads((md / "layout.json").read_text(encoding="utf-8"))
    cost = json.loads((md / "_cost.json").read_text(encoding="utf-8"))
    # 复现 s7 的切分，按去重后的顺序取回译文
    segs, _ = segments(layout, with_refs=False)
    uniq = list(dict.fromkeys(s["text"] for s in segs))
    trans = {t: cost["translations"].get(str(i)) for i, t in enumerate(uniq)}

    src = pymupdf.open(pdf_path)
    # 收集每页要放的译文块：(bbox, html, 字号, 粗体)
    placements = {p: [] for p in range(len(src))}
    carry = {}  # 跨页段落拆出来、要放到下一页空块里的后半段
    for pno, info in enumerate(layout["pdf_info"]):
        blocks = list(info["para_blocks"]) + [b for b in info["discarded_blocks"] if b["type"] == "page_footnote"]
        for b in blocks:
            for x in b.get("blocks", [b]):
                if x["type"] not in TRANSLATE_TYPES:
                    continue
                if not x.get("lines"):
                    if pno in carry:  # 上一页合并过来的续段，放到这个空块的位置
                        placements[pno].append((x["bbox"], *carry.pop(pno)))
                    continue
                text, formulas = block_text(x)
                zh = trans.get(text)
                if not zh:
                    continue
                y0, y1 = x["bbox"][1] - 5, x["bbox"][3] + 5
                inside = [ln for ln in x["lines"] if y0 <= ln["bbox"][1] <= y1]
                if len(inside) < len(x["lines"]):
                    ratio = len(inside) / len(x["lines"])
                    head, tail = split_at_ratio(zh, ratio)
                    zh = head
                    size, bold = block_style(src[pno], x["bbox"])
                    carry[pno + 1] = (render_inline(tail, formulas), size, bold)
                size, bold = block_style(src[pno], x["bbox"])
                placements[pno].append((x["bbox"], render_inline(zh, formulas), size, bold))

    # 背景页：只删译文块里的文字和完全落在块内的线条
    bg = pymupdf.open(pdf_path)
    for pno in pages:
        page = bg[pno]
        for bbox, *_ in placements[pno]:
            page.add_redact_annot(pymupdf.Rect(bbox) + (0.5, 0.5, -0.5, -0.5))
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    bg.select(pages)

    w, h = src[pages[0]].rect.width, src[pages[0]].rect.height
    sections = []
    for pno in pages:
        divs = "".join(
            f'<div class="blk" data-size="{size:.2f}" style="left:{b[0]}pt;top:{b[1]}pt;width:{b[2] - b[0]}pt;'
            f'height:{b[3] - b[1]}pt;font-weight:{"bold" if bold else "normal"}"><div>{body}</div></div>'
            for b, body, size, bold in placements[pno]
        )
        sections.append(f'<section class="page" style="width:{w}pt;height:{h}pt">{divs}</section>')
    doc_html = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{KATEX}/katex.min.css">
<script src="{KATEX}/katex.min.js"></script>
<style>
@page {{ size: {w}pt {h}pt; margin: 0 }}
html, body {{ margin: 0; background: transparent }}
.page {{ position: relative; overflow: hidden; break-after: page }}
.blk {{ position: absolute; font-family: 'WenQuanYi Zen Hei', sans-serif; line-height: 1.3; color: #000 }}
.blk > div {{ text-align: justify }}
.katex {{ font-size: 1.05em }}
</style></head><body>{''.join(sections)}</body></html>"""
    stem = Path(pdf_path).stem
    (OUT / f"{stem}.mineru.overlay.html").write_text(doc_html, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.set_content(doc_html, wait_until="networkidle")
        bad = pg.evaluate("""() => { let bad = 0;
            for (const el of document.querySelectorAll('.tex')) {
              try { katex.render(el.textContent, el, {throwOnError: true}); }
              catch (e) { bad++; el.style.color = '#c00'; }
            } return bad; }""")
        pg.evaluate("document.fonts.ready")
        fit = pg.evaluate(FIT_JS)
        pg.pdf(path=str(OUT / f"{stem}.mineru.overlay.pdf"), prefer_css_page_size=True, print_background=False)
        browser.close()

    overlay = pymupdf.open(OUT / f"{stem}.mineru.overlay.pdf")
    for i in range(len(bg)):
        bg[i].show_pdf_page(bg[i].rect, overlay, i)
    out_pdf = OUT / f"{stem}.mineru.translated.pdf"
    bg.save(out_pdf, garbage=3, deflate=True)
    res = pymupdf.open(out_pdf)
    for i, pno in enumerate(pages):
        res[i].get_pixmap(dpi=90).save(OUT / f"{stem}.p{pno}.mineru.out.png")
    print(json.dumps({
        "pages": pages,
        "blocks": sum(len(placements[p]) for p in pages),
        "katex_formulas": doc_html.count('class="tex"'),
        "katex_errors": bad,
        "fit_min_scale": round(min((f["scale"] for f in fit), default=1), 3),
        "overflow_blocks": sum(f["overflow"] for f in fit),
        "output_kb": out_pdf.stat().st_size // 1024,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:])
