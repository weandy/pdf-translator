"""与文字相关的纯函数。"""


def is_bad_char(c: str) -> bool:
    """替换符或私用区字符：通常是 PDF 字体缺少 ToUnicode 映射，提取出来的不是真实文字。"""
    if c == "�":
        return True
    cp = ord(c)
    return 0xE000 <= cp <= 0xF8FF
