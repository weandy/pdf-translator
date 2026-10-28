"""用 PyMuPDF 提取字符级坐标与样式（S4：PyMuPDF 的坐标与 MinerU layout.json 一致）。"""

from pathlib import Path
from typing import Any, cast

import pymupdf

from pdftr.domain.chars import Char, CharPage
from pdftr.domain.geometry import Rect
from pdftr.extraction.classify import classify_page

_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4
_BOLD_NAMES = ("Bold", "Black", "Heavy", "Semibold", "SemiBold", "Medi")


def extract(pdf: Path) -> list[CharPage]:
    pages: list[CharPage] = []
    with pymupdf.open(pdf) as doc:
        for page in doc:
            pages.append(_page(page))
    return pages


def _page(page: pymupdf.Page) -> CharPage:
    chars: list[Char] = []
    line_no = 0
    # PyMuPDF 的类型声明把 get_text 的返回值标成 str，"rawdict" 模式实际返回 dict
    raw = cast(dict[str, Any], page.get_text("rawdict"))
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                font = span["font"]
                bold = bool(span["flags"] & _FLAG_BOLD) or any(k in font for k in _BOLD_NAMES)
                italic = bool(span["flags"] & _FLAG_ITALIC) or "Italic" in font or "Oblique" in font
                for ch in span["chars"]:
                    chars.append(
                        Char(
                            c=ch["c"],
                            bbox=Rect.of(ch["bbox"]),
                            font=font,
                            size=span["size"],
                            color=span["color"],
                            bold=bold,
                            italic=italic,
                            line=line_no,
                        )
                    )
            line_no += 1
    rect = page.rect
    return CharPage(
        index=page.number or 0,
        width=rect.width,
        height=rect.height,
        rotation=page.rotation,
        kind=classify_page(page, chars),
        chars=chars,
    )


def page_count(pdf: Path) -> int:
    """打开失败（不是 PDF、已加密等）时抛 ValueError。"""
    try:
        with pymupdf.open(pdf) as doc:
            if doc.needs_pass:
                raise ValueError("PDF 已加密，请先移除密码")
            return len(doc)
    except (pymupdf.FileDataError, RuntimeError) as e:
        raise ValueError("无法打开文件，请确认是有效的 PDF") from e
