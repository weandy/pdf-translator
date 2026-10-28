"""逐页分类（docs/01-tech-stack.md §4.2）：原生文本 / 扫描 / 文字提取乱码。

第一阶段只处理原生页；扫描页和乱码页在界面上提示，由第三阶段的 OCR 路径处理。
"""

from collections.abc import Sequence

import pymupdf

from pdftr.domain.chars import Char
from pdftr.domain.ir import PageKind
from pdftr.domain.text import is_bad_char

MIN_CHARS = 20  # 少于这么多可见字符，视为“几乎没有文字”
SCAN_IMAGE_COVERAGE = 0.5  # 大图覆盖超过这个比例，视为扫描页
GARBLED_RATIO = 0.3  # 替换符/私用区字符超过这个比例，视为乱码页


def classify(chars: Sequence[Char], image_coverage: float) -> PageKind:
    visible = [ch.c for ch in chars if not ch.c.isspace()]
    if len(visible) < MIN_CHARS:
        return PageKind.SCANNED if image_coverage >= SCAN_IMAGE_COVERAGE else PageKind.NATIVE
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


def classify_page(page: pymupdf.Page, chars: Sequence[Char]) -> PageKind:
    return classify(chars, image_coverage(page))
