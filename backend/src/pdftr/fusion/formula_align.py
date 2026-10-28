"""块级解析结果的公式定位：只有块坐标与内容（行内公式写成 $LaTeX$、没有位置）时，
对照块内的 PDF 字符拆成行与片段，并推算每个行内公式的位置。

做法：把内容里公式之外的文字（字母数字与常用标点）与 PDF 字符序列对齐，两个已对上的锚点之间、
没被文字占用的那些 PDF 字符就是公式所在处，取其外接框。推算结果不合理（跨两行以上、字符远多于公式本身、
与相邻公式重叠）时放弃位置，该公式的字符按正文保留——宁可少一个公式，也不让一大块正文变成“公式”。
正文片段一律取自 PDF 字符（电子版论文的文字层比识别结果可靠）。
"""

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutSpan
from pdftr.fusion.text_fix import text_of

_FORMULA = re.compile(r"\$\$?([^$]+?)\$\$?")
_MAX_HEIGHT = 3.2  # 行内公式的高度最多约 3 个字号（跨一次行，或带上下标、分式）


def _key(c: str) -> str:
    """对齐锚点只用字母数字：标点在公式内部也大量出现（括号、逗号、省略号），用作锚点会对到公式里去。"""
    return c.lower() if c.isalnum() else ""


@dataclass
class Located:
    latex: str
    chars: list[int]  # 公式占用的 PDF 字符下标（块内）；空表示没找到位置


def _words(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[^\W_]+", text)]


def locate_formulas(content: str, chars: Sequence[Char], rows: Sequence[int] | None = None) -> list[Located]:
    """按内容里出现的顺序返回每个行内公式及其占用的 PDF 字符。rows 为各字符所在的视觉行（默认用 PyMuPDF 行号）。

    按词对齐：连续几个词组成的短语在段落里基本唯一，对齐算法会先锁定它们；按字符对齐时最长匹配只有一个词，
    重复出现的词（如两处“layers”）容易配错位置，后面整段错位。
    """
    pieces = _FORMULA.split(content)  # 偶数下标是文字，奇数下标是公式
    seq: list[str] = []
    owner: list[int] = []  # 每个词属于哪个文字片段
    for i in range(0, len(pieces), 2):
        for w in _words(pieces[i]):
            seq.append(w)
            owner.append(i)
    # PDF 的词：字母数字连续段，遇到其他字符或换行即断开（换行处常缺空格，如“respectively,with”）
    row = list(rows) if rows is not None else [c.line for c in chars]
    pdf: list[str] = []
    spans: list[list[int]] = []  # 每个词占用的字符下标
    for k, c in enumerate(chars):
        if _key(c.c):
            if spans and spans[-1][-1] == k - 1 and row[k] == row[k - 1]:
                pdf[-1] += _key(c.c)
                spans[-1].append(k)
            else:
                pdf.append(_key(c.c))
                spans.append([k])
    matcher = difflib.SequenceMatcher(None, seq, pdf, autojunk=False)
    matched: dict[int, list[int]] = {}  # 内容里的词下标 → 对上的 PDF 字符下标
    for a, b, n in matcher.get_matching_blocks():
        for k in range(n):
            matched[a + k] = spans[b + k]
    used = {k for idx in matched.values() for k in idx}
    sizes = sorted(c.size for c in chars)
    body = sizes[len(sizes) // 2] if sizes else 10.0  # 块的正文字号：公式里多是下标小字，不能拿公式自己的字号作基准

    # 每个公式落在“前后两个已对上的词之间”；中间没有词的相邻公式（如“$a$, $b$”）会分到同一段
    ranges: list[tuple[int, int]] = []
    for i in range(1, len(pieces), 2):
        before = [matched[j][-1] for j, o in enumerate(owner) if o < i and j in matched]
        after = [matched[j][0] for j, o in enumerate(owner) if o > i and j in matched]
        ranges.append((max(before) + 1 if before else 0, min(after) if after else len(chars)))

    out: list[Located] = []
    n = 0
    while n < len(ranges):
        group = n
        while group + 1 < len(ranges) and ranges[group + 1] == ranges[n]:
            group += 1
        lo, hi = ranges[n]
        free = [k for k in range(lo, hi) if k not in used and not chars[k].c.isspace()]
        separators = [pieces[2 * m + 2] for m in range(n, group)]  # 相邻公式之间的文字（只有标点空白）
        for m, part in zip(range(n, group + 1), _split(free, chars, separators), strict=True):
            latex = pieces[2 * m + 1].strip()
            part = _trim(part, chars, pieces[2 * m], pieces[2 * m + 2] if 2 * m + 2 < len(pieces) else "")
            part = _one_line(part, chars, row)
            out.append(Located(latex, part if _plausible(part, chars, latex, body) else []))
        n = group + 1
    return out


def _trim(part: list[int], chars: Sequence[Char], before: str, after: str) -> list[int]:
    """公式前后紧挨的标点（如句末的“.”、括号）属于正文：前文结尾、后文开头的标点从公式范围里还回去。"""
    part = list(part)
    lead = re.match(r"[^\w]*", after.lstrip()).group(0) if after else ""  # type: ignore[union-attr]
    for c in reversed([c for c in lead if not c.isspace()]):
        if part and chars[part[-1]].c == c:
            part.pop()
    trail = re.search(r"[^\w]*$", before.rstrip()).group(0) if before else ""  # type: ignore[union-attr]
    for c in [c for c in trail if not c.isspace()]:
        if part and chars[part[0]].c == c:
            part.pop(0)
    return part


def _one_line(part: list[int], chars: Sequence[Char], row: list[int]) -> list[int]:
    """公式主体只取一行（字母数字最多的那行）；只有真的换行时（主体延伸到行尾、下一行的部分从行首开始）
    才保留下一行的部分。否则相邻行混进来的字符会让公式框高出一两行。"""
    if not part:
        return part
    counts: dict[int, int] = {}
    for k in part:
        counts[row[k]] = counts.get(row[k], 0) + (1 if chars[k].c.isalnum() else 0)
    main = max(counts, key=lambda r: (counts[r], -r))
    keep = [k for k in part if row[k] == main]
    rest = [k for k in part if row[k] == main + 1]
    in_main = [k for k in range(len(chars)) if row[k] == main and chars[k].c.isalnum()]
    in_next = [k for k in range(len(chars)) if row[k] == main + 1 and chars[k].c.isalnum()]
    wraps = rest and keep and in_main and in_next and max(in_main) <= max(keep) and min(in_next) >= min(rest)
    return sorted(keep + rest) if wraps else keep


def _split(free: list[int], chars: Sequence[Char], separators: list[str]) -> list[list[int]]:
    """在一段字符里按分隔标点切成 len(separators)+1 份；找不到分隔处时只给第一个公式，其余为空。"""
    parts: list[list[int]] = []
    rest = free
    for sep in separators:
        mark = [c for c in sep if not c.isspace()]
        cut = next(
            (p for p in range(len(rest)) if mark and [chars[k].c for k in rest[p : p + len(mark)]] == mark), None
        )
        if cut is None:
            return [free] + [[] for _ in separators]
        parts.append(rest[:cut])
        rest = rest[cut + len(mark) :]
    return [*parts, rest]


def visual_lines(chars: Sequence[Char]) -> list[list[Char]]:
    """按位置重建视觉上的行：PyMuPDF 把上下标拆成单独的行，竖直中心与主行相距不到 0.6 个字号的并回主行。

    不能用字框重叠来判断：字框按字体上下边界计算，相邻两行正文的字框本来就互相重叠。
    """

    def center(cs: list[Char]) -> float:
        ys = sorted((c.bbox.y0 + c.bbox.y1) / 2 for c in cs)
        return ys[len(ys) // 2]

    by_line: dict[int, list[Char]] = {}
    for c in chars:
        by_line.setdefault(c.line, []).append(c)
    groups: list[list[list[Char]]] = []  # 每组若干 PyMuPDF 行，第一个是主行（字最多）
    for line in sorted(by_line.values(), key=center):
        size = sorted(c.size for c in line)[len(line) // 2]
        for g in groups:
            main = max(g, key=len)
            if abs(center(line) - center(main)) < 0.6 * max(size, sorted(c.size for c in main)[len(main) // 2]):
                g.append(line)
                break
        else:
            groups.append([line])
    rows = [sorted((c for line in g for c in line), key=lambda c: c.bbox.x0) for g in groups]
    return sorted(rows, key=lambda r: center(r))


def _plausible(span: list[int], chars: Sequence[Char], latex: str, body: float) -> bool:
    if not span:
        return False
    normal = [chars[k] for k in span if chars[k].bbox.height <= 1.6 * chars[k].size] or [chars[k] for k in span]
    height = max(c.bbox.y1 for c in normal) - min(c.bbox.y0 for c in normal)  # 根号等超高字框不计入
    # LaTeX 里的命令名（\mathbb、\frac）会多算字母，按 3 倍再加余量放宽；超出说明把正文也圈进来了
    letters = sum(1 for c in re.sub(r"\\[A-Za-z]+", "", latex) if c.isalnum())
    glyphs = sum(1 for k in span if chars[k].c.isalnum())
    return height <= _MAX_HEIGHT * body and glyphs <= 3 * letters + 4


def _box(chars: Sequence[Char]) -> Rect:
    return Rect(
        x0=min(c.bbox.x0 for c in chars),
        y0=min(c.bbox.y0 for c in chars),
        x1=max(c.bbox.x1 for c in chars),
        y1=max(c.bbox.y1 for c in chars),
    )


def _formula_box(part: Sequence[Char], row: Sequence[Char]) -> Rect:
    """公式框：上下边界只用字框高度正常的字形确定，左右边界用全部字形。

    根号、≲ 等数学符号的字框按数学字体上下边界计算，能盖住上下几行；框太高时，融合阶段会把落在框里的
    上下行正文当成公式字符排除掉，造成正文丢字。row 参数保留给调用方，便于以后按行修正。
    """
    del row
    box = _box(part)
    normal = [c for c in part if c.bbox.height <= 1.6 * c.size]
    if not normal:
        return box
    return Rect(x0=box.x0, y0=min(c.bbox.y0 for c in normal), x1=box.x1, y1=max(c.bbox.y1 for c in normal))


def _flush_text(run: list[Char], spans: list[LayoutSpan]) -> None:
    """把攒下的正文字符输出为一个文字片段并清空。"""
    # 同一视觉行内已按横坐标排好；text_of 会按原始行号重排（上下标单独成行会被排到后面），这里抹平行号
    text = text_of([c.model_copy(update={"line": 0}) for c in run]) if run else ""
    if text.strip():
        spans.append(LayoutSpan(kind="text", content=text, bbox=_box(run)))
    run.clear()


def refine_block(block: LayoutBlock, chars: Sequence[Char]) -> tuple[LayoutBlock, int, int]:
    """块级结果 → 带行与片段的块。返回（新块, 公式总数, 找到位置的公式数）。"""
    content = block.content or ""
    visible = [c for c in chars if not c.c.isspace()]
    if not visible:  # 没有文字层（扫描页等）：整块作为一个片段，公式按原样留在文字里
        span = LayoutSpan(kind="text", content=_FORMULA.sub(lambda m: m.group(1), content), bbox=block.bbox)
        return block.model_copy(update={"lines": [LayoutLine(bbox=block.bbox, spans=[span])]}), 0, 0

    rows = visual_lines(chars)
    ordered = [c for row in rows for c in row]
    row_of = {id(c): n for n, row in enumerate(rows) for c in row}
    located = locate_formulas(content, ordered, [row_of[id(c)] for c in ordered])
    owner = {k: n for n, f in enumerate(located) for k in f.chars}  # PDF 字符下标 → 公式编号

    lines: list[LayoutLine] = []
    emitted: set[int] = set()
    for line_no in range(len(rows)):
        idx = [k for k, c in enumerate(ordered) if row_of[id(c)] == line_no]
        spans: list[LayoutSpan] = []
        run: list[Char] = []
        for k in idx:
            n = owner.get(k)
            if n is None:
                run.append(ordered[k])
                continue
            _flush_text(run, spans)
            if n not in emitted:  # 跨行的公式只在第一行出一个片段，框取它在这一行的部分
                part = [ordered[j] for j in located[n].chars if row_of[id(ordered[j])] == line_no]
                spans.append(
                    LayoutSpan(kind="inline_formula", content=located[n].latex, bbox=_formula_box(part, rows[line_no]))
                )
                emitted.add(n)
        _flush_text(run, spans)
        line_chars = [ordered[k] for k in idx if not ordered[k].c.isspace()]
        if spans and line_chars:
            lines.append(LayoutLine(bbox=_box(line_chars), spans=spans))
    found = sum(1 for f in located if f.chars)
    return block.model_copy(update={"lines": lines}), len(located), found
