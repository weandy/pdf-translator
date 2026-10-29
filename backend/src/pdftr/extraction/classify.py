"""逐页分类（docs/01-tech-stack.md §4.2）：原生文本 / 扫描 / 文字提取乱码。

扫描页用解析服务识别出的文字（MinerU 对扫描件识别良好，实测与原文相似度 0.94–0.97）；乱码页提示并跳过。
网上的扫描版论文大多带一层隐形 OCR 文字（方便搜索复制，质量往往很差，如 “du_dw”“(lOb)”），
不能当成原生页：删掉的只是看不见的文字，图片上的原文还在，译文叠上去会重影。
"""

from collections.abc import Sequence

import pymupdf

from pdftr.domain.chars import Char
from pdftr.domain.ir import PageKind
from pdftr.domain.text import is_bad_char

MIN_CHARS = 20  # 少于这么多可见字符，视为“几乎没有文字”
SCAN_IMAGE_COVERAGE = 0.5  # 大图覆盖超过这个比例，视为扫描页
GARBLED_RATIO = 0.3  # 替换符/私用区字符超过这个比例，视为乱码页
INVISIBLE_RATIO = 0.5  # 隐形文字（渲染模式 3）占这个比例以上且有整页大图：带隐形 OCR 层的扫描页


def classify(chars: Sequence[Char], image_coverage: float, invisible_ratio: float = 0.0) -> PageKind:
    visible = [ch.c for ch in chars if not ch.c.isspace()]
    if len(visible) < MIN_CHARS:
        return PageKind.SCANNED if image_coverage >= SCAN_IMAGE_COVERAGE else PageKind.NATIVE
    if invisible_ratio >= INVISIBLE_RATIO and image_coverage >= SCAN_IMAGE_COVERAGE:
        return PageKind.SCANNED
    bad = sum(is_bad_char(c) for c in visible)
    if bad / len(visible) >= GARBLED_RATIO:
        return PageKind.GARBLED
    return PageKind.NATIVE


def image_coverage(page: pymupdf.Page) -> float:
    area = page.rect.width * page.rect.height
    if area <= 0:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        covered = max(covered, r.width * r.height)  # 取最大的一张图，扫描页通常是一整页图
    return covered / area


def invisible_ratio(page: pymupdf.Page) -> float:
    """页面文字里隐形文字（渲染模式 3，OCR 层常用）所占的比例。"""
    total = hidden = 0
    for span in page.get_texttrace():
        n = sum(1 for ch in span["chars"] if chr(ch[0]).strip())
        total += n
        hidden += n if span.get("type") == 3 else 0
    return hidden / total if total else 0.0


def classify_page(page: pymupdf.Page, chars: Sequence[Char]) -> PageKind:
    return classify(chars, image_coverage(page), invisible_ratio(page))
