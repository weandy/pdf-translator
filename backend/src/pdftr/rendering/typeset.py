"""译文排版规则（W3C《中文排版需求》clreq），网页阅读器与导出 PDF 共用（前端 shared/text/typeset.ts 与此一致）。

- §6.3.3 汉字与西文、数字之间留四分之一个汉字宽的间距；标点、括号旁不加。
  网页插入空格（西文空格约 0.25 字宽），导出 PDF 由排版器作为间距处理（rendering/typesetter.py）。
- §6.2.1.1 段首缩进两个汉字：只用于正文段落；列表项、公式后接着说的半句（原文以小写字母开头）不缩进。
- §6.2 正文两端对齐，末行左对齐。
"""

import re

from pdftr.domain.ir import Block, BlockType, Segment

_LIST_ITEM = re.compile(r"^\s*(?:[•·▪◦‣\-–—*]|\(?\d+\)|\d+[.)、]|\[\d+\]|[a-z][.)]\s)")

# 解析器偶尔把整行文字标成上下标（如列表项的第一行被包在 <sub> 里），真正的上下标都很短
_FAKE_SCRIPT = re.compile(r"<(sup|sub)>([^<]{13,})</\1>")

# 行距：放得下时优先宽松（英译中一般会省出空间），依次收紧，最后才缩小字号
LINE_HEIGHTS = (1.5, 1.4, 1.3, 1.2)


def drop_fake_scripts(text: str) -> str:
    """超过 12 个字符的“上下标”去掉标签，按正文显示（与前端 dropFakeScripts 一致）。"""
    return _FAKE_SCRIPT.sub(r"\2", text)


def wants_indent(block: Block, segment: Segment, first: bool) -> bool:
    """段首缩进两字：正文段落的开头部分；列表项与公式后接着说的半句不缩进。"""
    if block.type is not BlockType.TEXT or not first or block.style.bold:
        return False
    source = segment.source.lstrip()
    return not (_LIST_ITEM.match(source) or source[:1].islower())
