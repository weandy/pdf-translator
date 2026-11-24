"""与文字相关的纯函数。"""

import re

RICH_TAG = re.compile(r"</?(?:b|i|sup|sub)>")
RICH_ATOM = re.compile(r"</?(?:b|i|sup|sub)>|<m[0-9]+/>|[\s\S]")
INLINE_ABSTRACT = re.compile(r"^(?:abstract|index\s+terms|key\s*words?)\s*[.:：—–\-]\s*", re.I)
SECTION_NUMBER = re.compile(r"^(?:[IVXLCDM]+[.)]|\d+(?:\.\d+)*[.)])\s*", re.I)
ROMAN_HEADING = re.compile(r"^(?:X{1,3}(?:IX|IV|V?I{0,3})|IX|IV|V?I{1,3}|V)\.\s+\S")
NUMBERED_HEADING = re.compile(r"^([A-Z]|\d+)((?:\.\d+)*)\.?\s+\S")
LETTER_HEADING = re.compile(r"^[A-Z]\.\s+\S")
PUBLICATION_DATE = re.compile(r"^(?:received|revised|accepted|published|available\s+online)\s*[:：]", re.I)

_ACRONYM_TERM = re.compile(r"^[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)*s?$")
_ASCII_WORD_TERM = re.compile(r"^[A-Za-z0-9\s'-]+$")
_TERM_SPLIT = re.compile(r"[\s-]+")
_ES_PLURAL = re.compile(r"(?:s|x|z|ch|sh)es$")


def visible_length(text: str) -> int:
    return sum(RICH_TAG.fullmatch(m[0]) is None for m in RICH_ATOM.finditer(text))


def is_pua(c: str) -> bool:
    cp = ord(c)
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0x10FFFF


def is_bad_char(c: str) -> bool:
    """替换符或私用区字符：通常是 PDF 字体缺少 ToUnicode 映射，提取出来的不是真实文字。"""
    return c == "\ufffd" or is_pua(c)


def _norm_ascii_word(word: str) -> str:
    """小写化并做确定性简单复数归并，仅作用于普通 ASCII 词片。"""
    w = word.lower()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and _ES_PLURAL.search(w):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def normalize_term_key(term: str) -> str:
    """生成带显式词形类别的术语规范键（与 frontend/src/shared/text/termKey.ts 同口径）。

    - ``acr:``：保留大小写的全大写缩写及其复数（如 ``SVMs`` → ``acr:SVM``、``ADAM`` → ``acr:ADAM``）。
    - ``lit:``：含 ``/``、``@``、``.`` 等技术分隔符或非 ASCII 字符的字面键（如 ``TCP/IP`` → ``lit:tcp/ip``）。
    - ``word:``：普通 ASCII 词/短语，统一空格/连字符并做简单复数归并
      （如 ``policy-gradient`` → ``word:policy gradient``）。
    - 空串或纯空白返回空串，不入索引。
    """
    collapsed = " ".join(term.strip().split())
    if not collapsed:
        return ""
    if " " not in collapsed and _ACRONYM_TERM.fullmatch(collapsed):
        stem = collapsed[:-1] if collapsed.endswith("s") else collapsed
        if sum("A" <= ch <= "Z" for ch in stem) >= 2:
            return f"acr:{stem}"
    if not _ASCII_WORD_TERM.fullmatch(collapsed):
        return f"lit:{collapsed.casefold()}"
    parts = [p for p in _TERM_SPLIT.split(collapsed) if p]
    if not parts or not any(ch.isalnum() for p in parts for ch in p):
        return f"lit:{collapsed.casefold()}"
    return "word:" + " ".join(_norm_ascii_word(p) for p in parts)
