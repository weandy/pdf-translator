"""PDF 里的链接（引用跳转、图表与章节跳转、网址）在译文中的位置。

去字背景页会把译文块里的链接一起删掉（实测 ResNet 288 个只剩 27 个），双语对照拼页更是一个不剩。
译文的位置和原文不同，不能把链接框原样放回：先取每个链接覆盖的原文字（如“[22,”里的 22、网址），
在这一块的译文里找到对应文字，再在它实际画出来的位置加链接（网页阅读器同样按这段文字加链接）。

匹配都在“紧凑文本”上做：去掉公式占位符、格式标签与所有空白（排版器把空格变成字距，不画出来）。
"""

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

import pymupdf
from pydantic import BaseModel

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Document

_PLACEHOLDER = re.compile(r"<m\d+/>|</?(?:sup|sub|b|i)>")
_SPACE = re.compile(r"\s+")
_EDGE = " \t\n[]()（）【】,，;；:：."
_NUMBER = re.compile(r"\d+(?:[.\-–]\d+)*[a-z]?")
_LABELS = "图表节式算法附录定理引理推论定义章例"  # 中文译文里图表、章节编号前面的字
SKIP_DESTS = ("Hfootnote", "footnote", "page")  # 脚注、页码跳转：编号很短，译文里容易配错，不加


class PdfLink(BaseModel):
    page: int
    rect: Rect
    uri: str = ""
    dest_page: int | None = None
    dest_x: float = 0.0  # 目标位置，PDF 原始坐标（y 自下而上），写回 PDF 时原样使用
    dest_y: float = 0.0
    dest_top: float = 0.0  # 目标位置离页面顶部的距离（网页阅读器滚动用）
    anchor: str = ""  # 原文里链接覆盖的文字
    kind: str = ""  # 命名跳转的类型（cite、figure、section…），没有时为空
    block_id: str | None = None  # 落在哪个译文块里；None 表示在译文块外，原位置不变
    text: str = ""  # 译文里对应的紧凑文字；找不到时为空，译文里不加这个链接
    nth: int = 0  # 是这一块紧凑译文里第几处 text（从 0 起）


def compact(text: str) -> str:
    return _SPACE.sub("", _PLACEHOLDER.sub("", text))


def extract_links(pdf: Path, doc: Document, translated: Iterable[str]) -> list[PdfLink]:
    """读出原文 PDF 的全部链接；translated：被替换成译文的块 id（这些块里的链接要在译文里重新定位）。"""
    ids = set(translated)
    blocks = [b for b in doc.blocks() if b.id in ids]
    out: list[PdfLink] = []
    with pymupdf.open(pdf) as src:
        for page_no in range(len(src)):
            page = src[page_no]
            height = page.rect.height
            for raw in page.get_links():
                r = raw["from"]
                kind = (raw.get("nameddest") or "").split(".")[0]
                link = PdfLink(page=page_no, rect=Rect(x0=r.x0, y0=r.y0, x1=r.x1, y1=r.y1),
                               anchor=page.get_textbox(r).strip(), kind=kind)  # fmt: skip
                if raw["kind"] == pymupdf.LINK_URI:
                    link.uri = raw.get("uri") or ""
                elif raw.get("page", -1) is not None and raw.get("page", -1) >= 0:
                    to = raw.get("to") or pymupdf.Point(0, height)
                    link.dest_page, link.dest_x, link.dest_y = raw["page"], to.x, to.y
                    link.dest_top = max(src[raw["page"]].rect.height - to.y, 0.0)
                else:
                    continue  # 打开文件、启动程序等其他链接不保留
                cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                owner = next((b for b in blocks if b.page == page_no and b.bbox.x0 <= cx <= b.bbox.x1
                              and b.bbox.y0 <= cy <= b.bbox.y1), None)  # fmt: skip
                link.block_id = owner.id if owner else None
                out.append(link)
    return out


def _occurrences(text: str, needle: str) -> list[int]:
    out, i = [], text.find(needle)
    while i >= 0 and needle:
        out.append(i)
        i = text.find(needle, i + 1)
    return out


def _numbers(text: str, number: str) -> list[int]:
    """text 里作为完整编号出现的 number（前后不紧挨数字、小数点）。"""
    pat = re.compile(rf"(?<![\d.]){re.escape(number)}(?!\d|\.\d)")
    return [m.start() for m in pat.finditer(text)]


def _in_brackets(text: str, pos: int) -> bool:
    """pos 是否在一对括号里：编号引用用方括号，作者-年份引用用圆括号（译文里可能是全角）。

    跨栏段落拆成几块后，左括号可能在上一块里，所以只看到右括号（中间没有左括号）也算。
    """
    for opens, closes in (("[［", "]］"), ("(（", ")）")):
        left = max(text.rfind(c, 0, pos) for c in opens)
        if left >= 0 and max(text.rfind(c, 0, pos) for c in closes) < left:
            return True
        rights = [i for c in closes if (i := text.find(c, pos)) >= 0]
        if left < 0 and rights and not any(text.find(c, pos, min(rights)) >= 0 for c in opens):
            return True
    return False


def _candidates(link: PdfLink, text: str) -> tuple[str, list[int]]:
    """（要加链接的文字, 它在 text 里可能的位置）。"""
    if link.uri:  # 先找完整网址（链接文字可能被换行截断、带着脚注编号或句末标点），再找去掉协议的网址、链接文字
        for core in (link.uri, re.sub(r"^https?://", "", link.uri), compact(link.anchor).rstrip(".,;)）")):
            if core and (spots := _occurrences(text, core)):
                return core, spots
        return "", []
    core = compact(link.anchor).strip(_EDGE)
    if not core or link.kind in SKIP_DESTS:
        return "", []
    if core.endswith("-") and len(core) > 3:  # 行末断词（如“Rad-”），下一行的“ford et al.”是另一个链接
        core = core[:-1]
    if _NUMBER.fullmatch(core):
        spots = _numbers(text, core)
        if link.kind == "cite":  # 引用编号、年份只在括号里找，不配到正文里的数字
            return core, [p for p in spots if _in_brackets(text, p)]
        labelled = [p for p in spots if any(c in _LABELS for c in text[max(p - 3, 0) : p])]
        return core, labelled or spots
    spots = _occurrences(text, core)
    if not spots and link.kind == "cite":  # 作者-年份引用：“et al.”可能被译成“等”，退而找姓氏
        surname = re.match(r"[A-Za-z][A-Za-z'\-]{2,}", core)
        if surname:
            return surname.group(0), _occurrences(text, surname.group(0))
    return core, spots


def locate(
    links: Sequence[PdfLink], pieces: dict[str, str], siblings: dict[str, list[str]] | None = None
) -> list[PdfLink]:
    """在每块的译文（pieces：块 id → 这一块的译文）里定位链接文字，填 text 与 nth。

    同一块里的链接按原文阅读顺序依次分配，同一处文字只给一个链接；译文语序会变，所以先找上一处之后的，找不到再从头找。
    siblings：块 id → 同一段落的各块（跨栏、跨页段落按译文长度拆开，引用可能落在相邻那块），本块找不到时去那里找。
    """
    out = [link.model_copy() for link in links]
    used: dict[str, set[tuple[int, int]]] = {}
    last: dict[str, int] = {}

    def take(link: PdfLink, block_id: str) -> bool:
        text = compact(pieces.get(block_id, ""))
        needle, spots = _candidates(link, text)
        taken = used.setdefault(block_id, set())
        free = [p for p in spots if not any(a < p + len(needle) and p < b for a, b in taken)]
        if not needle or not free:
            return False
        pos = next((p for p in free if p >= last.get(block_id, 0)), free[0])
        taken.add((pos, pos + len(needle)))
        last[block_id] = pos + len(needle)
        link.block_id, link.text, link.nth = block_id, needle, _occurrences(text, needle).index(pos)
        return True

    for link in sorted(out, key=lambda lk: (lk.page, round(lk.rect.y0), lk.rect.x0)):
        if link.block_id is None or take(link, link.block_id):
            continue
        for other in (siblings or {}).get(link.block_id, []):
            if other != link.block_id and take(link, other):
                break
    return out
