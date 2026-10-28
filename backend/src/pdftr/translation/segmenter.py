"""IR → 翻译单元（Segment）。

- 沿 continues_from 链把跨栏/跨页段落合并成一个单元（M2 已把续行放回各自的块）
- 行尾断词合并：“representa-” + “tion” → “representation”；复合词“left-to-” + “right”保留连字符
- 行内公式换成 <mN/>，编号在单元内重排，LaTeX 存在 formulas 里
- 参考文献默认不翻（S7：占可翻文字 15%–26%），可由选项开启
"""

import re
from collections.abc import Iterable

from pdftr.domain.ids import segment_id
from pdftr.domain.ir import Block, BlockType, Document, FormulaRef, Segment, SegmentPart

# MinerU 会把序数词的上标（如 1^{st}、2^{nd}）识别成公式；模型会正确地把它译成“一阶”“二阶”，
# 占位符随之“消失”导致校验失败（M3 实测）。这类公式还原成普通文字再翻译。
_ORDINAL = re.compile(r"^(\d+)(st|nd|rd|th)$")

MIN_WORDS = 1  # 没有英文单词（纯数字、符号）的块不翻译；单词标题如 “Introduction” 要翻


def _join(prev: str, nxt: str) -> str:
    """拼接两行文字，处理行尾断词。"""
    if not prev:
        return nxt
    if not nxt:
        return prev
    m = re.search(r"([A-Za-z][\w-]*)-$", prev)
    if m and nxt[0].islower():
        word = m.group(1)
        if "-" in word:  # 复合词在连字符处换行：保留连字符
            return prev + nxt
        return prev[:-1] + nxt
    return f"{prev} {nxt}"


def textual_formula(latex: str) -> str | None:
    """能直接写成文字的“公式”（目前只有序数词，如 1^{st} → 1st），否则返回 None。"""
    plain = re.sub(r"\\(mathrm|text|textrm)|[\s{}^]", "", latex)
    return plain if _ORDINAL.match(plain) else None


def block_source(block: Block, key_map: dict[str, str]) -> str:
    """块内片段按行拼成一段文字；行内公式换成重排后的占位符（能写成文字的直接写文字）。"""
    latex = {f.key: f.latex for f in block.formulas}
    lines: dict[int, list[str]] = {}
    for s in block.spans:
        if s.kind == "formula" and s.formula:
            key = key_map.get(s.formula)
            piece = f"<{key}/>" if key else (textual_formula(latex.get(s.formula, "")) or "")
        else:
            piece = s.text.strip()
        if piece:
            lines.setdefault(s.line, []).append(piece)
    text = ""
    for li in sorted(lines):
        text = _join(text, " ".join(lines[li]))
    return re.sub(r"\s+", " ", text).strip()


def _chain(head: Block, next_of: dict[str, Block]) -> list[Block]:
    chain = [head]
    seen = {head.id}
    while (nxt := next_of.get(chain[-1].id)) and nxt.id not in seen:
        chain.append(nxt)
        seen.add(nxt.id)
    return chain


def _wanted(block: Block, include_references: bool) -> bool:
    if block.type is BlockType.REFERENCE:
        return include_references and any(s.text.strip() for s in block.spans)
    return block.translatable


def segment(
    doc: Document,
    *,
    include_references: bool = False,
    pages: Iterable[int] | None = None,
) -> list[Segment]:
    page_set = set(pages) if pages is not None else None
    next_of = {b.continues_from: b for b in doc.blocks() if b.continues_from}
    segments: list[Segment] = []
    section = ""
    for block in doc.blocks():
        if block.type is BlockType.TITLE:
            section = (
                block_source(block, {f.key: f.key for f in block.formulas if not textual_formula(f.latex)}) or section
            )
        if block.continues_from or not _wanted(block, include_references):
            continue  # 续接块随链头一起处理
        if page_set is not None and block.page not in page_set:
            continue
        chain = _chain(block, next_of)
        formulas: dict[str, str] = {}
        refs: dict[str, FormulaRef] = {}
        parts: list[SegmentPart] = []
        text = ""
        for b in chain:
            key_map = {}
            for f in b.formulas:
                if textual_formula(f.latex):
                    continue  # 不占编号，block_source 里直接写成文字
                key = f"m{len(formulas) + 1}"
                key_map[f.key] = key
                formulas[key] = f.latex
                refs[key] = FormulaRef(page=b.page, bbox=f.bbox)
            piece = block_source(b, key_map)
            parts.append(SegmentPart(block_id=b.id, chars=len(piece)))
            text = _join(text, piece)
        if len(re.findall(r"[A-Za-z]{2,}", re.sub(r"<m\d+/>", " ", text))) < MIN_WORDS:
            continue
        segments.append(
            Segment(
                id=segment_id(doc.job_id, len(segments)),
                job_id=doc.job_id,
                block_ids=[b.id for b in chain],
                source=text,
                formulas=formulas,
                formula_refs=refs,
                parts=parts,
                section=section if block.type is not BlockType.TITLE else "",
            )
        )
    return segments
