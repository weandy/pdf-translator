"""论文标题识别与下载文件名规则，只依赖已解析的文档结构。"""

import html
import re
import unicodedata
from collections.abc import Sequence

from pdftr.domain.ir import Block, BlockType, Page

_SECTION = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+|[A-Z][.)]\s+|"
    r"(?:abstract|introduction|references|bibliography|acknowledg(?:e)?ments?|"
    r"contents|keywords?|appendix|摘要|引言|参考文献)[:：]?$)",
    re.IGNORECASE,
)
_PUBLICATION = re.compile(
    r"^(?:(?:[\w&'-]+\s+){0,6}journal\s+of\b|international\s+journal\b|proceedings\s+of\b|"
    r"article\s+(?:info(?:rmation)?|history)\b|research\s+article\b|review\s+article\b|"
    r"original\s+(?:research|article)\b|accepted\s+manuscript\b|available\s+online\b|"
    r"contents\s+lists?\b|www\.|https?://|doi\s*:|arxiv\s*:|issn\b|copyright\b|©)",
    re.IGNORECASE,
)
_MASTHEAD = re.compile(
    r"^(?:Nature|Science|ScienceDirect|Springer(?:\s+Nature)?|Elsevier|Wiley|Scientific\s+Reports)$", re.I
)
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
_SECTION_LABEL = re.compile(
    r"^(?:methods?|methodology|results?|discussion|conclusions?|related\s+work|background|"
    r"experimental\s+(?:setup|results)|材料与方法|结论|结果|讨论)[:：]?$",
    re.IGNORECASE,
)
MAX_DOWNLOAD_NAME_BYTES = 200  # 为常见文件系统的255字节分量上限留出浏览器重名后缀空间


def _clean(text: str) -> str:
    text = html.unescape(re.sub(r"</?(?:b|i|sup|sub)>", "", text))
    text = "".join(c for c in text if unicodedata.category(c) not in {"Cc", "Cf"} or c.isspace())
    return " ".join(unicodedata.normalize("NFC", text).split())


def _block_title(block: Block) -> str:
    formulas = {f.key: f.latex for f in block.formulas}
    return _clean(" ".join(s.text if s.kind == "text" else formulas.get(s.formula or "", "") for s in block.spans))


def _candidate(text: str) -> bool:
    return (
        4 <= len(text) <= 500
        and any(c.isalpha() for c in text)
        and not (_SECTION.match(text) or _SECTION_LABEL.fullmatch(text) or _PUBLICATION.match(text))
    )


def paper_title_blocks(pages: Sequence[Page]) -> list[Block]:
    """优先首两页前部显著标题；相邻、同字号的标题块可合并，正文和作者块会截断合并。"""
    for page in sorted(pages, key=lambda p: p.index)[:2]:
        blocks = sorted(page.blocks, key=lambda b: b.order)
        first_section = next(
            (i for i, b in enumerate(blocks) if b.type is BlockType.TITLE and _SECTION.match(_block_title(b))),
            len(blocks),
        )
        candidates = [
            (i, b)
            for i, b in enumerate(blocks)
            if i < first_section
            and b.type is BlockType.TITLE
            and b.bbox.y0 < page.height * 0.75
            and _candidate(_block_title(b))
            and not (b.bbox.y0 < page.height * 0.1 and _MASTHEAD.fullmatch(_block_title(b)))
        ]
        if not candidates:
            if first_section < len(blocks):
                break  # 摘要/正文已开始，不能把后页章节标题补成论文标题。
            continue
        # 主标题通常最醒目；同字号时取靠前的一处，不以字符串长度偏向正文。
        index, anchor = max(candidates, key=lambda item: (item[1].style.font_size, -item[1].bbox.y0))
        eligible = {b.id for _, b in candidates}
        group = [anchor]
        for direction in (-1, 1):
            previous = anchor
            i = index + direction
            while 0 <= i < len(blocks):
                block = blocks[i]
                upper, lower = (block, previous) if direction < 0 else (previous, block)
                size = max(anchor.style.font_size, 1)
                overlap = min(block.bbox.x1, previous.bbox.x1) - max(block.bbox.x0, previous.bbox.x0)
                if (
                    block.id not in eligible
                    or abs(block.style.font_size - size) > size * 0.15
                    or not -size * 0.2 <= lower.bbox.y0 - upper.bbox.y1 <= size * 1.5
                    or overlap <= 0
                ):
                    break
                group.append(block)
                previous = block
                i += direction
        return sorted(group, key=lambda b: b.order)
    return []


def paper_title(pages: Sequence[Page], fallback: str = "") -> str:
    """读取主标题；旧IR只有meta.title可用时保留该值，不从正文臆测标题。"""
    blocks = paper_title_blocks(pages)
    if blocks:
        return _clean(" ".join(_block_title(b) for b in blocks))
    cleaned = _clean(fallback)
    return cleaned if _candidate(cleaned) and not _MASTHEAD.fullmatch(cleaned) else ""


def download_filename(title: str, uploaded_name: str, *, bilingual: bool) -> str:
    """保留各语言字母数字，空白、标点和路径符统一为下划线；限制UTF-8总字节数。"""
    suffix = "_双语对照.pdf" if bilingual else "_译文.pdf"
    original = re.split(r"[\\/]", uploaded_name)[-1]
    original = re.sub(r"\.pdf$", "", original, flags=re.IGNORECASE)
    prefix = ""
    for value in (title, original, "document"):
        clean = unicodedata.normalize("NFKC", _clean(value))
        # 删除不可见控制字符后再归一分隔符，避免方向控制符伪造扩展名。
        prefix = "".join(c if unicodedata.category(c)[0] in {"L", "N", "M"} else "_" for c in clean)
        prefix = re.sub(r"_+", "_", prefix).strip("_")
        if prefix:
            break
    if _RESERVED.fullmatch(prefix):
        prefix = f"_{prefix}"
    budget = MAX_DOWNLOAD_NAME_BYTES - len(suffix.encode("utf-8"))
    prefix = prefix.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip("_")
    return f"{prefix or 'document'}{suffix}"
