"""以 PyMuPDF 提取的文字纠正解析器文字——只修“确定坏了”的文字。

M2 在 5 篇论文上试过通用纠正（两者相似就用 PDF 文字），结果利弊参半：
能修好“ε 变成控制字符”“L2 丢了 2”，但也会把相邻行公式的重音符、求和号拼进来，
或丢掉单独成行的章节编号和标点。翻译原文被改坏的代价更大，所以只在解析器文字
含控制字符、替换符或私用区字符（必然是坏的）时，才改用 PDF 文字。
“≲ 1 被识别成 b1”这类看起来正常的识别错误暂不处理（见 docs/03-mvp-design.md）。
"""

import difflib
import re
import unicodedata
from collections.abc import Sequence

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.text import is_bad_char

MIN_SIMILARITY = 0.75
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def chars_in(chars: Sequence[Char], box: Rect, exclude: Sequence[Rect] = (), tol: float = 1.0) -> list[Char]:
    """框内、且不在行内公式框里的字符。"""
    return [
        ch
        for ch in chars
        if box.contains_point(*ch.bbox.center, tol) and not any(e.contains_point(*ch.bbox.center, 0.5) for e in exclude)
    ]


def text_of(chars: Sequence[Char]) -> str:
    """按 PyMuPDF 行、横坐标排序后拼接；字符间距明显时补空格。"""
    ordered = sorted(chars, key=lambda c: (c.line, c.bbox.x0))
    parts: list[str] = []
    prev: Char | None = None
    for ch in ordered:
        if prev is not None:
            new_line = ch.line != prev.line
            gap = ch.bbox.x0 - prev.bbox.x1
            if (new_line or gap > 0.25 * ch.size) and parts and not parts[-1].endswith(" ") and ch.c != " ":
                parts.append(" ")
        parts.append(ch.c)
        prev = ch
    text = unicodedata.normalize("NFKC", "".join(parts))  # 连字 ﬁ → fi 等
    return re.sub(r"\s+", " ", text).strip()


def is_broken(text: str) -> bool:
    return bool(_CONTROL.search(text)) or any(is_bad_char(c) for c in text)


def choose(parser_text: str, pdf_text: str) -> tuple[str, bool]:
    """返回 (采用的文字, 是否采用了 PDF 文字)。"""
    if not parser_text.strip():
        return (pdf_text, True) if pdf_text and not is_broken(pdf_text) else (parser_text, False)
    if not is_broken(parser_text) or not pdf_text or is_broken(pdf_text):
        return parser_text, False
    a = re.sub(r"\s+", "", _CONTROL.sub("", parser_text)).lower()
    b = re.sub(r"\s+", "", pdf_text).lower()
    if difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() < MIN_SIMILARITY:
        return parser_text, False
    return pdf_text, True
