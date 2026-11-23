"""与文字相关的纯函数。"""

import re

RICH_TAG = re.compile(r"</?(?:b|i|sup|sub)>")
RICH_ATOM = re.compile(r"</?(?:b|i|sup|sub)>|<m[0-9]+/>|[\s\S]")
INLINE_ABSTRACT = re.compile(r"^(?:abstract|index\s+terms|keywords)\s*[—–\-:：]\s*", re.I)
SECTION_NUMBER = re.compile(r"^(?:[IVXLCDM]+[.)]|\d+(?:\.\d+)*[.)])\s*", re.I)
ROMAN_HEADING = re.compile(r"^(?:X{1,3}(?:IX|IV|V?I{0,3})|IX|IV|V?I{1,3}|V)\.\s+\S")
NUMBERED_HEADING = re.compile(r"^([A-Z]|\d+)((?:\.\d+)*)\.?\s+\S")
LETTER_HEADING = re.compile(r"^[A-Z]\.\s+\S")
PUBLICATION_DATE = re.compile(r"^(?:received|revised|accepted|published|available\s+online)\s*[:：]", re.I)


def visible_length(text: str) -> int:
    return sum(RICH_TAG.fullmatch(m[0]) is None for m in RICH_ATOM.finditer(text))


def is_pua(c: str) -> bool:
    cp = ord(c)
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0x10FFFF


def is_bad_char(c: str) -> bool:
    """替换符或私用区字符：通常是 PDF 字体缺少 ToUnicode 映射，提取出来的不是真实文字。"""
    return c == "�" or is_pua(c)
