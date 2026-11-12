"""用 PyMuPDF 提取字符级坐标与样式（S4：PyMuPDF 的坐标与 MinerU layout.json 一致）。"""

from pathlib import Path
from typing import Any, cast

import pymupdf

from pdftr.domain.chars import Char, CharPage
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import PageKind
from pdftr.extraction.classify import classify_page

_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4
# Medium 是常规字重（项目生成的黑体文件也使用这个名字），不能按粗体处理。
_BOLD_NAMES = ("Bold", "Black", "Heavy", "Semibold", "SemiBold")


def _font_is_bold(font: str) -> bool:
    """按字体名补充 PyMuPDF 可能漏报的粗体标记。"""
    return any(name in font for name in _BOLD_NAMES)


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
            dx, dy = line.get("dir", (1.0, 0.0))
            upright = abs(dx - 1) < 0.01 and abs(dy) < 0.01
            for span in line["spans"]:
                font = span["font"]
                bold = bool(span["flags"] & _FLAG_BOLD) or _font_is_bold(font)
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
                            upright=upright,
                        )
                    )
            line_no += 1
    rect = page.rect
    kind = classify_page(page, chars)
    return CharPage(
        index=page.number or 0,
        width=rect.width,
        height=rect.height,
        rotation=page.rotation,
        kind=kind,
        chars=[] if kind is PageKind.SCANNED else chars,  # 扫描页的隐形 OCR 文字不可靠，一律用解析服务的识别结果
    )


def page_count(pdf: Path | bytes) -> int:
    """打开失败（不是 PDF、已加密等）时抛 ValueError。"""
    try:
        # 上传已有完整字节，直接校验内存流，避免 Windows 临时文件共享与坏文件句柄问题。
        with pymupdf.open(stream=pdf, filetype="pdf") if isinstance(pdf, bytes) else pymupdf.open(pdf) as doc:
            if doc.needs_pass:
                raise ValueError("PDF 已加密，请先移除密码")
            return len(doc)
    except (pymupdf.FileDataError, RuntimeError) as e:
        raise ValueError("无法打开文件，请确认是有效的 PDF") from e
