"""译文排版规则（W3C《中文排版需求》clreq），网页阅读器与导出 PDF 共用（前端 shared/text/typeset.ts 与此一致）。

- §6.3.3 汉字与西文、数字之间留四分之一个汉字宽的间距；标点、括号旁不加。
  网页插入空格（西文空格约 0.25 字宽），导出 PDF 由排版器作为间距处理（rendering/typesetter.py）。
- §6.2.1.1 段首缩进两个汉字：只用于正文段落；列表项不缩进。
- §6.2 正文两端对齐，末行左对齐。
"""

import re

from pdftr.domain.ir import Block, BlockType, Segment

_LIST_ITEM = re.compile(r"^\s*(?:[•·▪◦‣\-–—*]|\(?\d+\)|\d+[.)、]|\[\d+\]|[a-z][.)]\s)")

# 阈值与前端相同，空白集合和长度均按 Unicode 码点计算。
_SCRIPT = re.compile(r"<(sup|sub)>([^<]{13,})</\1>")
_SCRIPT_SPACE = re.compile(r"[ \t\r\n\f\v\u00a0\u3000]")
_CJK_SENTENCE = re.compile(r"^[\u3400-\u9fff]{20,}[。！？]$")

# 行距：放得下时优先宽松（英译中一般会省出空间），依次收紧，最后才缩小字号
LINE_HEIGHTS = (1.5, 1.4, 1.3, 1.2)


_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
_WESTERN = r"(?:[A-Za-z0-9]|<m\d+/>)"
_FORMAT_TAGS = r"(?:</?(?:b|i|sup|sub)>)*"
_MIXED_SPACE = re.compile(
    rf"({_HAN.pattern}{_FORMAT_TAGS})[ \t\u00a0](?={_FORMAT_TAGS}{_WESTERN})"
    rf"|({_WESTERN}{_FORMAT_TAGS})[ \t\u00a0](?={_FORMAT_TAGS}{_HAN.pattern})"
)


def normalize_cjk_spacing(text: str) -> str:
    """移除汉字与西文、数字、公式间的单个横向空格，保留富文本标签。"""
    return _MIXED_SPACE.sub(lambda match: match.group(1) or match.group(2), text)


def drop_fake_scripts(text: str) -> str:
    """去掉明显覆盖整行或句子的上下标，保留技术标识符。"""

    def strip_fake(match: re.Match[str]) -> str:
        content = match.group(2)
        return content if _SCRIPT_SPACE.search(content) or _CJK_SENTENCE.fullmatch(content) else match.group(0)

    return _SCRIPT.sub(strip_fake, text)


def wants_indent(block: Block, segment: Segment, first: bool) -> bool:
    """段首缩进两字：普通正文的第一块；列表项、标题和图注不缩进。"""
    if block.type is not BlockType.TEXT or not first:
        return False
    source = segment.source.lstrip()
    return not _LIST_ITEM.match(source)
