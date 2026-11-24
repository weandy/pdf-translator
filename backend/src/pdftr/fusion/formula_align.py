"""块级解析结果的公式定位：只有块坐标与内容（行内公式写成 $LaTeX$、没有位置）时，
对照块内的 PDF 字符拆成行与片段，并推算每个行内公式的位置。

做法：把内容里公式之外的文字（字母数字与常用标点）与 PDF 字符序列对齐，两个已对上的锚点之间、
没被文字占用的那些 PDF 字符就是公式所在处，取其外接框。推算结果不合理（跨两行以上、字符远多于公式本身、
与相邻公式重叠）时放弃位置，该公式的字符按正文保留——宁可少一个公式，也不让一大块正文变成“公式”。
正文片段一律取自 PDF 字符（电子版论文的文字层比识别结果可靠）。
"""

import difflib
import re
import statistics
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutSpan
from pdftr.domain.text import RICH_TAG
from pdftr.fusion.text_fix import chars_in, main_metrics, script_glyph, text_of

_FORMULA = re.compile(r"\$\$?([^$]+?)\$\$?")
_MAX_HEIGHT = 3.2  # 行内公式的高度最多约 3 个字号（跨一次行，或带上下标、分式）
_NORMAL_GLYPH_HEIGHT = 2.0  # Elsevier 正文和下标的字框约为字号的 1.64 倍。


_CONNECTIVE = r"(?:If|When|Where|Then|And|Or|For|With|Let)"
_LEADING_TEXT = re.compile(
    rf"^(?:\\(?:mathrm|text|textrm|operatorname)\s*\{{([A-Za-z\s]+)\}}|({_CONNECTIVE})(?=\s|$))\s*"
)
_SPLIT_WORD = re.compile(
    r"^(?:\\(?:mathrm|text)\s*\{\s*(\{\s*[A-Za-z]\s*\}|[A-Za-z])\s*\}|([A-Za-z]))"
    r"\s*(?:\{\s*:\s*\}|:|\\colon(?![A-Za-z]))\s*"
)
_TRAILING_MARK = re.compile(r"(?<![\\.,;:])(?:\\colon|(?:\\(?:mathrm|text)\s*)?\{\s*:\s*\}|([,;:.]))$")
_TRAILING_FRAGMENT = re.compile(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z]*$")
_LATIN_ABBR = r"(?:i\s*\.\s*e\s*\.|e\s*\.\s*g\s*\.)"
_LEADING_PROSE = re.compile(
    rf"^(?:(?P<open>(?:\\left\s*)?\(\s*)?"
    rf"(?:\\(?:mathrm|text|textrm|operatorname)\s*\{{\s*(?P<wrapped_latin>{_LATIN_ABBR})(?P<in_comma>\s*,)?\s*\}}"
    rf"(?P<out_comma>\s*,)?|(?P<bare_latin>{_LATIN_ABBR})(?P<bare_comma>\s*,)?)(?P<sep>\s*)|"
    r"(?P<reference>(?:E\s*q\s*\.|\\(?:mathrm|text|operatorname)\s*\{\s*E\s*q\s*\.\s*\})"
    r"\s*(?:\\left\s*)?\(\s*\d+(?:\s*[a-z])?\s*(?:\\right\s*)?\)\s*,)\s*)"
)
_INNER_CONDITION = re.compile(
    r"(?<![A-Za-z\\])"
    r"(?:\\(?:mathrm|text|textrm|operatorname)\s*\{\s*(?P<w>i\s*f|w\s*h\s*e\s*n|w\s*h\s*e\s*r\s*e)\s*"
    r"(?P<ic>:|\\colon)?\s*\}\s*(?P<oc>:|\\colon)?|"
    r"(?P<bw>i\s*f|w\s*h\s*e\s*n|w\s*h\s*e\s*r\s*e)\s*(?P<bc>:|\\colon))\s*",
    re.I,
)
_TRAILING_RELATION = re.compile(
    r"(?<![=<>!:\\])(?P<op>=|\\geq(?![A-Za-z])|\\ge(?![A-Za-z])|≥|\\leq(?![A-Za-z])|\\le(?![A-Za-z])|≤|>|<)$"
)
_SCALAR_START = re.compile(r"^\s*[+-−]?\d+(?:\.\d+)?%?(?![A-Za-z])")
_RELATION_TEXT = {
    "=": "=",
    r"\geq": "≥",
    r"\ge": "≥",
    "≥": "≥",
    r"\leq": "≤",
    r"\le": "≤",
    "≤": "≤",
    ">": ">",
    "<": "<",
}
_RELATION_CHARS = ("=", "≥", "≤", ">", "<")
_HAS_MATH_ATOM = re.compile(r"[A-Za-z0-9\\α-ωΑ-Ω]")
_TAIL_SCRIPT = re.compile(r";\s*(?:\{\s*\^\s*(?:\{(\d+)\}|(\d+))\s*\}|\^\s*\{(\d+)\}|\(\s*(\d+)\s*\))$")
_TAIL_POSSESSIVE = re.compile(r"(?:'|\{\s*'\s*\}|\\prime|\^\s*\{\s*\\prime\s*\})\s*s$")
_TAIL_WORD = re.compile(r"\\(?:mathrm|text)\s*\{\s*([a-z](?:\s*[a-z]){2,})\s*\}$")
_TAIL_SUB_SEMICOLON = re.compile(r"_\s*\{\s*;\s*\}$")
MATH_WORDS = frozenset(
    "arccos arcsin arctan arg cos cosh cot coth csc deg det dim erf exp gcd inf ker lcm lim liminf limsup "
    "log max min mod Pr prob rank sec sign sgn sin sinh sup tan tanh trace tr grad div curl diag span "
    "abs ceil floor norm card var cov std mean med median argmax argmin const "
    "mol rad deg rpm min sec hour day byte bit kwh mwh mpa kpa mhz khz ghz ohm dba ppm ppb".lower().split()
)
_TEX_TOKEN = re.compile(r"\\(?:begin|end)\{[^{}]+\}|\\(?:left|right)\s*[.|]|\\(?:[a-zA-Z]+|.)|.", re.S)
_OPEN = {
    "(": ")",
    "[": "]",
    "{": "}",
    r"\{": r"\}",
    r"\langle": r"\rangle",
    r"\lvert": r"\rvert",
    r"\lVert": r"\rVert",
    r"\left|": r"\right|",
}
_CLOSE = frozenset(_OPEN.values())
_EDGE_OPEN = re.compile(r"^(?:\\left\s*)?([(\[])")
_EDGE_CLOSE = re.compile(r"(?:\\(?:left|right)\s*)?([()\[\]])$")
_INTERVAL = re.compile(r"^[\[(][^;]*,[^;]*[)\]]$")
MATH_FONT = re.compile(
    r"(?:STIX.*Math|CMSY|CMMI|CMEX|CMR\d|LMMath|LatinModernMath|CambriaMath|MathJax|Symbol|MSAM|MSBM)", re.I
)


def _delimiters(latex: str) -> tuple[set[int], list[tuple[int, str]]]:
    """扫描 LaTeX 外层边界。转义间距和环境内的分号不属于正文。"""
    stack: list[tuple[int, str, str]] = []
    surface: set[int] = set()
    unmatched: list[tuple[int, str]] = []
    for match in _TEX_TOKEN.finditer(latex):
        token, pos = match[0].replace(" ", "") if match[0].startswith("\\") else match[0], match.start()
        if not stack:
            surface.add(pos)
        if token in _OPEN:
            stack.append((pos, token, _OPEN[token]))
        elif token.startswith(r"\begin{"):
            stack.append((pos, token, r"\end{" + token[7:]))
        elif token in _CLOSE or token.startswith(r"\end{"):
            if stack and (
                stack[-1][2] == token
                or (token in {")", "]"} and stack[-1][1] in {"(", "["} and "," in latex[stack[-1][0] : pos])
            ):
                stack.pop()
            else:
                unmatched.append((pos, token))
    unmatched.extend((pos, token) for pos, token, _ in stack)
    return surface, unmatched


def _independent(latex: str) -> bool:
    if not latex or _delimiters(latex)[1] or re.search(r"^[=<>;,*/]|[=<>;,+\-*/]$", latex):
        return False
    return bool(re.search(r"\d|[=<>_^+*/−-]|\\[A-Za-z]+|[()[\]]", latex) or re.fullmatch(r"[A-Za-zα-ωΑ-Ω]", latex))


def _split_inner_condition(latex: str) -> list[str]:
    """拆分公式内部吞入的条件词+冒号（如 H_0 if: F \\geq），仅作用于外层且左右括号分别完整。"""
    surface, _ = _delimiters(latex)
    for match in _INNER_CONDITION.finditer(latex):
        if match.start() not in surface or not (match["ic"] or match["oc"] or match["bc"]):
            continue
        left = latex[: match.start()].rstrip()
        right = latex[match.end() :].lstrip()
        if (
            not left
            or not right
            or left.endswith(("_", "^", "{", "\\"))
            or not _HAS_MATH_ATOM.search(left)
            or not _HAS_MATH_ATOM.search(right)
            or _delimiters(left)[1]
            or _delimiters(right)[1]
        ):
            continue
        word = "".join((match["w"] or match["bw"]).split())
        return [left, f" {word}: ", *_split_inner_condition(right)]
    return [latex]


def _boundary_parts(latex: str) -> tuple[str, list[str], str]:
    prefix = suffix = ""
    if (lead := _LEADING_PROSE.match(latex)) and lead.end() < len(latex):
        if lead["wrapped_latin"] or lead["bare_latin"]:
            has_comma = bool(lead["in_comma"] or lead["out_comma"] or lead["bare_comma"])
            rest = latex[lead.end() :]
            if (has_comma or bool(lead["sep"])) and _HAS_MATH_ATOM.search(rest):
                latin = "".join((lead["wrapped_latin"] or lead["bare_latin"]).split())
                prefix = ("(" if lead["open"] else "") + latin + ("," if has_comma else "") + " "
                latex = rest
        else:
            ref = "".join(lead["reference"].split())
            ref = re.sub(r"\\(?:mathrm|text|operatorname)\{Eq\.\}", "Eq.", ref)
            ref = re.sub(r"\\(?:left|right)", "", ref)
            prefix = re.sub(r"Eq\.\((\d+[a-z]?)\)", r"Eq. (\1)", ref) + " "
            latex = latex[lead.end() :]
    surface, _ = _delimiters(latex)
    for pattern in (_TAIL_SCRIPT, _TAIL_POSSESSIVE, _TAIL_WORD):
        tail = pattern.search(latex)
        if tail is None or tail.start() not in surface:
            continue
        body = latex[: tail.start()].rstrip()
        if not body or body.endswith(("_", "^", "\\")):
            continue
        if pattern is _TAIL_SCRIPT:
            number = next(n for n in tail.groups() if n is not None)
            suffix = f"; ({number})" if tail[4] else f"; <sup>{number}</sup>"
        elif pattern is _TAIL_POSSESSIVE:
            suffix = "'s"
        else:
            word = "".join(tail[1].split())
            if word in MATH_WORDS or body.endswith((r"\,", r"\;", r"\!")):
                continue
            suffix = " " + word
        latex = body
        break
    if (sub_semi := _TAIL_SUB_SEMICOLON.search(latex)) and sub_semi.start() in surface and sub_semi.start() > 0:
        latex, suffix = latex[: sub_semi.start()].rstrip(), ";" + suffix
    if latex and not latex.endswith(r"\right.") and not _INTERVAL.fullmatch(latex):
        _, unmatched = _delimiters(latex)
        leading = _EDGE_OPEN.match(latex)
        if leading and (leading.start(1), leading[1]) in unmatched and leading.end() < len(latex):
            prefix += leading[1]
            latex = latex[leading.end() :].lstrip()
        _, unmatched = _delimiters(latex)
        closing = _EDGE_CLOSE.search(latex)
        if closing and (closing.start(1), closing[1]) in unmatched and closing.start() > 0:
            latex, suffix = latex[: closing.start()].rstrip(), closing[1] + suffix
    cond_items = _split_inner_condition(latex)
    if len(cond_items) > 1:
        return prefix, cond_items, suffix
    surface, _ = _delimiters(latex)
    cuts = [i for i, char in enumerate(latex) if char == ";" and i in surface]
    bounds = [-1, *cuts, len(latex)]
    pieces = [latex[a + 1 : b].strip() for a, b in pairwise(bounds)]
    if cuts and all(map(_independent, pieces)):
        alternating: list[str] = [pieces[0]]
        for piece in pieces[1:]:
            alternating.extend(("; ", piece))
        return prefix, alternating, suffix
    return prefix, [latex], suffix


def _strip_trailing_relation(latex: str, after: str) -> tuple[str, str]:
    """当公式末尾残留孤立关系符且右侧紧跟纯数字/简单标量时，将关系符剥离为正文。"""
    if not _SCALAR_START.match(RICH_TAG.sub("", after)):
        return latex, after
    match = _TRAILING_RELATION.search(latex)
    if match is None:
        return latex, after
    surface, _ = _delimiters(latex)
    if match.start() not in surface:
        return latex, after
    body = latex[: match.start()].rstrip()
    if not body or body.endswith(("_", "^", "{", "\\", "=", "<", ">", "!", ":")) or not _HAS_MATH_ATOM.search(body):
        return latex, after
    op_text = _RELATION_TEXT[match["op"]]
    new_after = (op_text + after) if after[:1].isspace() else f"{op_text} {after}"
    return body, new_after


def _content_parts(content: str, unchanged: dict[int, int] | None = None, *, trailing_after: str = "") -> list[str]:
    """先还原被公式边界吞掉的正文，供字符对齐和扫描页共用。"""
    raw = _FORMULA.split(content)
    parts = [raw[0]]
    for i in range(1, len(raw), 2):
        latex, after = raw[i].strip(), raw[i + 1]
        original = latex
        if lead := _LEADING_TEXT.match(latex):
            word = "".join((lead.group(1) or lead.group(2)).split())
            if re.fullmatch(_CONNECTIVE, word):
                parts[-1] = " ".join(filter(None, (parts[-1].rstrip(), word))) + " "
                latex = latex[lead.end() :]
        before = parts[-1].rstrip()
        if _TRAILING_FRAGMENT.search(before) and (lead := _SPLIT_WORD.match(latex)):
            letter = (lead.group(1) or lead.group(2)).strip("{}").strip()
            if letter.isupper() and lead.end() < len(latex):
                parts[-1] = before + letter + ": "
                latex = latex[lead.end() :]
        if mark := _TRAILING_MARK.search(latex):
            body = latex[: mark.start()].rstrip()
            if body and mark.start() in _delimiters(latex)[0] and not body.endswith(("_", "^", "{", "\\")):
                after = (mark.group(1) or ":") + after
                latex = body
                if latex.isdecimal():
                    parts[-1] += latex
                    latex = ""
        if latex:
            prefix, items, suffix = _boundary_parts(latex)
            if not suffix:
                if after.strip() or not trailing_after or i + 2 < len(raw):
                    items[-1], after = _strip_trailing_relation(items[-1], after)
                else:
                    stripped_item, synthetic = _strip_trailing_relation(items[-1], trailing_after)
                    if stripped_item != items[-1]:
                        items[-1] = stripped_item
                        op_text = synthetic[: len(synthetic) - len(trailing_after)].strip()
                        if op_text:
                            after = f" {op_text}"
            parts[-1] += prefix
            if unchanged is not None and items == [original]:
                unchanged[len(parts) // 2] = i // 2
            parts.extend(items[:-1])
            parts.extend((items[-1], suffix + after))
        else:
            parts[-1] += after
    return parts


def _key(c: str) -> str:
    """对齐锚点只用字母数字：标点在公式内部也大量出现（括号、逗号、省略号），用作锚点会对到公式里去。"""
    return c.lower() if c.isalnum() else ""


@dataclass
class Located:
    latex: str
    chars: list[int]  # 公式占用的 PDF 字符下标（块内）；空表示没找到位置


def _words(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[^\W_]+", RICH_TAG.sub("", text))]


def locate_formulas(
    content: str,
    chars: Sequence[Char],
    rows: Sequence[int] | None = None,
    *,
    trailing_after: str = "",
) -> list[Located]:
    """按内容里出现的顺序返回每个行内公式及其占用的 PDF 字符。rows 为各字符所在的视觉行（默认用 PyMuPDF 行号）。

    按词对齐：连续几个词组成的短语在段落里基本唯一，对齐算法会先锁定它们；按字符对齐时最长匹配只有一个词，
    重复出现的词（如两处“layers”）容易配错位置，后面整段错位。
    """
    pieces = _content_parts(content, trailing_after=trailing_after)  # 偶数下标是文字，奇数下标是公式
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
    before, after = RICH_TAG.sub("", before), RICH_TAG.sub("", after)
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
        surface, _ = _delimiters("".join(chars[k].c for k in rest))
        candidates = [p for p in surface if mark and [chars[k].c for k in rest[p : p + len(mark)]] == mark]
        cut = candidates[0] if len(candidates) == 1 else None
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
        return statistics.median(c.bbox.center[1] for c in cs)

    by_line: dict[int, list[Char]] = {}
    for c in chars:
        by_line.setdefault(c.line, []).append(c)
    groups: list[list[list[Char]]] = []  # 每组若干 PyMuPDF 行，第一个是主行（字最多）
    metrics = {id(line): main_metrics(line) for line in by_line.values()}
    for line in sorted(by_line.values(), key=center):
        size, baseline = metrics[id(line)]
        for g in reversed(groups):
            main = max(g, key=len)
            main_size, main_baseline = metrics[id(main)]
            gap = (
                abs(baseline - main_baseline)
                if baseline is not None and main_baseline is not None
                else abs(center(line) - center(main))
            )
            if gap < 0.6 * max(size, main_size):
                g.append(line)
                break
            if center(line) - center(main) > 2 * max(size, main_size):
                groups.append([line])
                break
        else:
            groups.append([line])
    rows = [sorted((c for line in g for c in line), key=lambda c: c.bbox.x0) for g in groups]
    return sorted(rows, key=lambda r: center(r))


def _plausible(span: list[int], chars: Sequence[Char], latex: str, body: float) -> bool:
    if not span:
        return False
    normal = [chars[k] for k in span if chars[k].bbox.height <= _NORMAL_GLYPH_HEIGHT * chars[k].size]
    normal = normal or [chars[k] for k in span]
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


def _formula_box(part: Sequence[Char], centers: Sequence[float], first: int, last: int) -> Rect:
    """左右覆盖全部字形，上下按字框和相邻视觉行的中线共同约束。"""
    box = _box(part)
    normal = _box([c for c in part if c.bbox.height <= _NORMAL_GLYPH_HEIGHT * c.size] or part)
    top, bottom = normal.y0, normal.y1
    if first:
        top = max(top, (centers[first - 1] + centers[first]) / 2)
    if last + 1 < len(centers):
        bottom = min(bottom, (centers[last] + centers[last + 1]) / 2)
    return Rect(x0=box.x0, y0=top, x1=box.x1, y1=bottom) if top < bottom else normal


def _flush_text(run: list[Char], spans: list[LayoutSpan]) -> None:
    """把攒下的正文字符输出为一个文字片段并清空。"""
    # 同一视觉行内已按横坐标排好；text_of 会按原始行号重排（上下标单独成行会被排到后面），这里抹平行号
    text = text_of([c.model_copy(update={"line": 0}) for c in run]) if run else ""
    if text.strip():
        spans.append(LayoutSpan(kind="text", content=text, bbox=_box(run)))
    run.clear()


_TORN_DELIMITERS = {("(", ")"), ("[", "]"), (r"\{", r"\}"), (r"\langle", r"\rangle")}
_BRIDGE_OPERATORS = ",;:=+-−*/<>≤≥·×|"
_BRIDGE_VECTOR_NEIGHBORS = frozenset(_BRIDGE_OPERATORS) - {";", ":"}
_BRIDGE_SYMBOL = f"[{re.escape(_BRIDGE_OPERATORS)}]"
_BRIDGE_TERM = r"\d+(?:\.\d+)?|[A-Za-z]{1,8}"
_MATH_BRIDGE = re.compile(
    rf"{_BRIDGE_SYMBOL}|{_BRIDGE_SYMBOL}?\s*(?:{_BRIDGE_TERM})"
    rf"(?:\s*{_BRIDGE_SYMBOL}\s*(?:{_BRIDGE_TERM})){{0,2}}\s*{_BRIDGE_SYMBOL}?"
)
_BRIDGE_WORD = re.compile(r"[A-Za-z]+")
_BRIDGE_STOP_WORDS = frozenset(
    {"and", "or", "if", "in", "of", "to", "as", "is", "are", "be", "by", "for", "with", "from", "where", "when",
     "then", "than", "the", "that", "this", "not", "vs"}
)  # fmt: skip
_BRIDGE_MATH_WORDS = MATH_WORDS - {"m", "g", "s"}
_BRIDGE_LATEX = str.maketrans({"−": "-", "×": r"\times ", "·": r"\cdot ", "≤": r"\le ", "≥": r"\ge "})


def _complementary_delimiters(left: str, right: str) -> bool:
    _, opens = _delimiters(left)
    _, closes = _delimiters(right)
    return len(opens) == len(closes) == 1 and (opens[0][1], closes[0][1]) in _TORN_DELIMITERS


def _math_bridge_latex(text: str, chars: Sequence[Char]) -> str | None:
    glyphs = [
        glyph
        for glyph in sorted(chars, key=lambda c: (c.line, c.bbox.x0))
        for char in unicodedata.normalize("NFKC", glyph.c)
        if not char.isspace()
    ]
    pieces: list[str] = []
    end = 0
    for match in _BRIDGE_WORD.finditer(text):
        word = match[0]
        if word.lower() in _BRIDGE_STOP_WORDS:
            return None
        before = text[: match.start()].rstrip()[-1:]
        after = text[match.end() :].lstrip()[:1]
        neighbors = {before, after}
        offset = len("".join(text[: match.start()].split()))
        token_chars = glyphs[offset : offset + len(word)]
        if len(word) == 1 and token_chars[0].bold and neighbors & _BRIDGE_VECTOR_NEIGHBORS:
            rendered = rf"\mathbf{{{word}}}"
        elif (word.lower() in _BRIDGE_MATH_WORDS and neighbors.intersection(_BRIDGE_OPERATORS)) or all(
            MATH_FONT.search(c.font) or c.italic for c in token_chars
        ):
            rendered = word
        else:
            return None
        pieces.extend((text[end : match.start()].translate(_BRIDGE_LATEX), rendered))
        end = match.end()
    pieces.append(text[end:].translate(_BRIDGE_LATEX))
    return "".join(pieces).rstrip()


def merge_torn_formulas(line: LayoutLine, chars: Sequence[Char]) -> LayoutLine:
    """只合并两端括号互补且中间字形唯一归属的公式。"""
    spans = line.spans.copy()
    body_size: float | None = None
    i = 0
    while i + 2 < len(spans):
        left, middle, right = spans[i : i + 3]
        bridge = middle.content.strip()
        if (
            (left.kind, middle.kind, right.kind) == ("inline_formula", "text", "inline_formula")
            and left.located and right.located
            and len(bridge) <= 16 and _MATH_BRIDGE.fullmatch(bridge)
            and left.bbox.center[0] < middle.bbox.center[0] < right.bbox.center[0]
            and _complementary_delimiters(left.content, right.content)
        ):  # fmt: skip
            boxes = (left.bbox, middle.bbox, right.bbox)
            if body_size is None:
                sizes = [glyph.size for glyph in chars_in(chars, line.bbox, tol=0)]
                if not sizes:
                    return line
                body_size = statistics.median(sizes)
            if not all(
                -0.3 * body_size <= b.bbox.x0 - a.bbox.x1 <= 1.5 * body_size for a, b in pairwise((left, middle, right))
            ):
                i += 1
                continue
            owned: list[list[Char]] = [[], [], []]
            box = Rect.of(
                (min(b.x0 for b in boxes), min(b.y0 for b in boxes), max(b.x1 for b in boxes), max(b.y1 for b in boxes))
            )
            for glyph in chars_in(chars, box, tol=0):
                if glyph.c.isspace():
                    continue
                matches = [n for n, b in enumerate(boxes) if b.contains_point(*glyph.bbox.center)]
                if len(matches) != 1:
                    break
                owned[matches[0]].append(glyph)
            else:
                inner = owned[1]
                if (
                    all(owned)
                    and text_of(inner).replace(" ", "") == "".join(bridge.split())
                    and len(visual_lines([c for group in owned for c in group])) == 1
                    and (latex_bridge := _math_bridge_latex(bridge, inner)) is not None
                ):
                    latex = " ".join((left.content.rstrip(), latex_bridge, right.content.lstrip()))
                    if not _delimiters(latex)[1]:
                        spans[i : i + 3] = [left.model_copy(update={"content": latex, "bbox": box})]
        i += 1
    return line.model_copy(update={"spans": spans})


_NATIVE_FONT_CMD = re.compile(r"\\(?:mathrm|text|textrm|operatorname|mathsf|mathbf|mathit|rm|sf)\s*\{([^{}\\]*)\}")
_NATIVE_FONT_GROUP = re.compile(r"\{\s*\\(?:sf|rm|it|bf)\s+([^{}\\]*)\}")
_NATIVE_COLON_UNIT = re.compile(r"\\colon(?![A-Za-z])|\{\s*:\s*\}|:")
_ITALIC_MATH_FONT = re.compile(r"CMMI", re.I)


def _nearest_center(centers: Sequence[float], y: float) -> int:
    return min(range(len(centers)), key=lambda i: abs(centers[i] - y))


def _scan_latex_surface_units(latex: str) -> list[tuple[str, int, int, str]]:
    """提取 LaTeX 外层单元：(去空白文本, start, end, kind)，kind ∈ {'word', 'colon', 'other'}。"""
    units: list[tuple[str, int, int, str]] = []
    n = len(latex)
    i = 0
    depth = 0
    env_depth = 0
    while i < n:
        if latex[i].isspace():
            i += 1
            continue
        if latex.startswith(r"\begin{", i):
            close = latex.find("}", i + 7)
            if close < 0:
                break
            env_depth += 1
            i = close + 1
            continue
        if latex.startswith(r"\end{", i):
            close = latex.find("}", i + 5)
            if close < 0:
                break
            if env_depth > 0:
                env_depth -= 1
            i = close + 1
            continue
        if env_depth > 0:
            i += 1
            continue
        ch = latex[i]
        if ch in "([":
            depth += 1
            units.append((ch, i, i + 1, "other"))
            i += 1
            continue
        if ch in ")]":
            if depth > 0:
                depth -= 1
            units.append((ch, i, i + 1, "other"))
            i += 1
            continue
        if depth > 0:
            i += 1
            continue
        if ch in "_^":
            start = i
            i += 1
            while i < n and latex[i].isspace():
                i += 1
            if i < n and latex[i] == "{":
                brace = 1
                i += 1
                while i < n and brace > 0:
                    if latex[i] == "\\" and i + 1 < n:
                        i += 2
                        continue
                    if latex[i] == "{":
                        brace += 1
                    elif latex[i] == "}":
                        brace -= 1
                    i += 1
            elif i < n and latex[i] == "\\":
                m_cmd = re.match(r"\\(?:[A-Za-z]+|.)", latex[i:])
                i += len(m_cmd[0]) if m_cmd else 1
            elif i < n:
                i += 1
            if units:
                prev_text, prev_s, _, _ = units[-1]
                units[-1] = (prev_text, prev_s, i, "other")
            else:
                units.append((latex[start:i], start, i, "other"))
            continue
        if m := _NATIVE_FONT_CMD.match(latex, i):
            inner = "".join(m[1].split())
            kind = "colon" if inner == ":" else "word" if re.fullmatch(r"[A-Za-z]+:?", inner) else "other"
            units.append((inner, i, m.end(), kind))
            i = m.end()
            continue
        if m := _NATIVE_FONT_GROUP.match(latex, i):
            inner = "".join(m[1].split())
            kind = "colon" if inner == ":" else "word" if re.fullmatch(r"[A-Za-z]+:?", inner) else "other"
            units.append((inner, i, m.end(), kind))
            i = m.end()
            continue
        if m := _NATIVE_COLON_UNIT.match(latex, i):
            units.append((":", i, m.end(), "colon"))
            i = m.end()
            continue
        if ch == "\\":
            m_cmd = re.match(r"\\(?:[A-Za-z]+|.)", latex[i:])
            end = i + (len(m_cmd[0]) if m_cmd else 1)
            units.append((latex[i:end], i, end, "other"))
            i = end
            continue
        if ch.isalpha():
            units.append((ch, i, i + 1, "word"))
            i += 1
            continue
        units.append((ch, i, i + 1, "other"))
        i += 1
    return units


def _split_latex_at_if(latex: str, *, has_colon: bool, has_left: bool) -> tuple[str, str] | None:
    """按外层词法单元将公式 LaTeX 在条件词 if:/it:/i: 或 if/it/i 处切分为左右两段。"""
    units = _scan_latex_surface_units(latex)
    if not units:
        return None
    targets = ("if:", "it:", "i:") if has_colon else ("if", "it", "i")
    ranked: list[tuple[int, str, str]] = []
    for a in range(len(units)):
        if units[a][3] != "word":
            continue
        for b in range(a + 1, min(len(units), a + 3) + 1):
            window = units[a:b]
            if any(u[3] not in {"word", "colon"} for u in window):
                break
            raw_joined = "".join(u[0] for u in window)
            joined = raw_joined.lower()
            if joined not in targets:
                continue
            word_part = raw_joined.rstrip(":")
            # 防止把小写 'i' 与紧随的大写数学变量 'F'/'T'（如 "i F \geq"）误合并成 "if"/"it"。
            if len(word_part) == 2 and word_part[1].isupper() and not (word_part[0].isupper() and len(window) == 1):
                continue
            cut_start, cut_end = window[0][1], window[-1][2]
            if a > 0 and units[a - 1][3] == "word" and units[a - 1][2] == cut_start:
                continue
            if not has_colon and b < len(units) and units[b][3] == "word" and cut_end == units[b][1]:
                continue
            left = latex[:cut_start].strip()
            right = latex[cut_end:].strip()
            if not right or not _HAS_MATH_ATOM.search(right) or _delimiters(right)[1]:
                continue
            if has_left:
                if not left or left.endswith(("_", "^", "{", "\\")) or not _HAS_MATH_ATOM.search(left):
                    continue
                if _delimiters(left)[1]:
                    continue
            elif left:
                continue
            ranked.append((targets.index(joined), left, right))
    if not ranked:
        return None
    best_rank = min(item[0] for item in ranked)
    best = [item for item in ranked if item[0] == best_rank]
    # 误识为单字母 i 时若出现多处歧义，保守放弃。
    if targets[best_rank] in {"i", "i:"} and len(best) != 1:
        return None
    return best[0][1], best[0][2]


def _tokenize_native_chars(chars: Sequence[Char]) -> list[tuple[str, list[Char]]]:
    ordered = sorted((c for c in chars if not c.c.isspace()), key=lambda c: c.bbox.x0)
    if not ordered:
        return []
    groups: list[list[Char]] = [[ordered[0]]]
    for prev, curr in pairwise(ordered):
        if curr.bbox.x0 - prev.bbox.x1 > 0.18 * max(prev.size, curr.size):
            groups.append([curr])
        else:
            groups[-1].append(curr)
    tokens: list[tuple[str, list[Char]]] = []
    for group in groups:
        word = unicodedata.normalize("NFKC", "".join(c.c for c in group))
        if (
            word == ":"
            and tokens
            and tokens[-1][0].lower() == "if"
            and group[0].bbox.x0 - tokens[-1][1][-1].bbox.x1 <= 0.35 * max(group[0].size, tokens[-1][1][-1].size)
        ):
            tokens[-1] = (tokens[-1][0] + ":", [*tokens[-1][1], *group])
        else:
            tokens.append((word, group))
    return tokens


def _split_native_if_formulas(line: LayoutLine, chars: Sequence[Char], block_centers: Sequence[float]) -> LayoutLine:
    """原生页公式内若 PDF 字符明确包含独立词 if:/if（即便 MinerU 错识成 it: 或吞掉 f），按字符坐标拆开。"""
    if not chars or not any(s.kind == "inline_formula" and s.located for s in line.spans):
        return line
    line_chars = [c for c in chars_in(chars, line.bbox, tol=1.0) if not c.c.isspace()]
    if not line_chars:
        return line
    line_size, line_baseline = main_metrics(line_chars)
    changed = False
    out: list[LayoutSpan] = []
    for span in line.spans:
        if span.kind != "inline_formula" or not span.located or not span.content.strip():
            out.append(span)
            continue
        f_chars = [c for c in chars_in(chars, span.bbox, tol=0.5) if not c.c.isspace()]
        if len(f_chars) < 3 or len(visual_lines(f_chars)) != 1:
            out.append(span)
            continue
        tokens = _tokenize_native_chars(f_chars)
        matches = [
            idx
            for idx, (word, word_chars) in enumerate(tokens)
            if word.lower() in {"if:", "if"} and all(not script_glyph(c, line_size, line_baseline) for c in word_chars)
        ]
        if len(matches) != 1:
            out.append(span)
            continue
        mid_idx = matches[0]
        cond_word, cond_chars = tokens[mid_idx]
        has_colon = cond_word.endswith(":")
        left_chars = [c for _, grp in tokens[:mid_idx] for c in grp]
        right_chars = [c for _, grp in tokens[mid_idx + 1 :] for c in grp]
        if not right_chars:
            out.append(span)
            continue
        if not has_colon:
            if any(c.italic or _ITALIC_MATH_FONT.search(c.font) for c in cond_chars):
                out.append(span)
                continue
            if right_chars[0].bbox.x0 - cond_chars[-1].bbox.x1 <= 0.15 * cond_chars[-1].size:
                out.append(span)
                continue
        split_latex = _split_latex_at_if(span.content.strip(), has_colon=has_colon, has_left=bool(left_chars))
        if split_latex is None:
            out.append(span)
            continue
        left_latex, right_latex = split_latex
        row_y = statistics.median(c.bbox.center[1] for c in f_chars)
        row_idx = _nearest_center(block_centers, row_y) if block_centers else None
        if left_chars:
            left_box = (
                _formula_box(left_chars, block_centers, row_idx, row_idx) if row_idx is not None else _box(left_chars)
            )
            out.append(LayoutSpan(kind="inline_formula", content=left_latex, bbox=left_box, located=True))
        out.append(LayoutSpan(kind="text", content=cond_word, bbox=_box(cond_chars)))
        right_box = (
            _formula_box(right_chars, block_centers, row_idx, row_idx) if row_idx is not None else _box(right_chars)
        )
        out.append(LayoutSpan(kind="inline_formula", content=right_latex, bbox=right_box, located=True))
        changed = True
    return line.model_copy(update={"spans": out}) if changed else line


def _clamp_line_formulas(line: LayoutLine, chars: Sequence[Char], block_centers: Sequence[float]) -> LayoutLine:
    """用整块视觉行中线约束单行行内公式的上下边界，防止高数学字框扎入上下邻行。"""
    if len(block_centers) < 2 or not any(s.kind == "inline_formula" and s.located for s in line.spans):
        return line
    text_chars = [
        c for s in line.spans if s.kind == "text" for c in chars_in(chars, s.bbox, tol=0.5) if not c.c.isspace()
    ]
    ref_y = statistics.median(c.bbox.center[1] for c in text_chars) if text_chars else line.bbox.center[1]
    row_idx = _nearest_center(block_centers, ref_y)
    lo = (block_centers[row_idx - 1] + block_centers[row_idx]) / 2 if row_idx > 0 else float("-inf")
    hi = (block_centers[row_idx] + block_centers[row_idx + 1]) / 2 if row_idx + 1 < len(block_centers) else float("inf")
    changed = False
    out: list[LayoutSpan] = []
    for span in line.spans:
        if span.kind != "inline_formula" or not span.located:
            out.append(span)
            continue
        top = max(span.bbox.y0, lo)
        bottom = min(span.bbox.y1, hi)
        if top < bottom and (top > span.bbox.y0 or bottom < span.bbox.y1):
            out.append(span.model_copy(update={"bbox": Rect(x0=span.bbox.x0, y0=top, x1=span.bbox.x1, y1=bottom)}))
            changed = True
        else:
            out.append(span)
    return line.model_copy(update={"spans": out}) if changed else line


def _relation_counts(parts: Iterable[str]) -> dict[str, int]:
    joined = "".join(RICH_TAG.sub("", part) for part in parts)
    return {op: joined.count(op) for op in _RELATION_CHARS}


def clean_formula_lines(block: LayoutBlock, chars: Sequence[Char]) -> LayoutBlock:
    """MinerU 已给行级公式时，只重新定位确实吞字的行，其余沿用原坐标。"""
    block_centers = [statistics.median(c.bbox.center[1] for c in row) for row in visual_lines(chars)] if chars else []
    lines: list[LayoutLine] = []
    for li, line in enumerate(block.lines):
        if len(line.spans) >= 3 and chars:
            line = merge_torn_formulas(line, chars)
        if chars:
            line = _split_native_if_formulas(line, chars, block_centers)
        if not any(span.kind == "inline_formula" for span in line.spans) or any(
            span.kind == "text" and "$" in span.content for span in line.spans
        ):
            lines.append(_clamp_line_formulas(line, chars, block_centers))
            continue
        next_line = block.lines[li + 1] if li + 1 < len(block.lines) else None
        next_text = (
            next_line.spans[0].content if next_line and next_line.spans and next_line.spans[0].kind == "text" else ""
        )
        content = " ".join(
            f"${span.content.strip()}$" if span.kind == "inline_formula" else span.content
            for span in line.spans
            if span.kind == "text" or span.content.strip()
        )
        unchanged: dict[int, int] = {}
        cleaned = _content_parts(content, unchanged, trailing_after=next_text)
        if cleaned == _FORMULA.split(content):
            lines.append(_clamp_line_formulas(line, chars, block_centers))
            continue
        line_chars = chars_in(chars, line.bbox, tol=1.0)
        refined, total, found = refine_block(
            block.model_copy(update={"bbox": line.bbox, "content": content}),
            line_chars,
            block_centers=block_centers,
            trailing_after=next_text,
        )
        # 原框仍比无锚点的猜测可靠，扫描页则保留新分出的文字和公式。
        scripts = _script_payloads(cleaned[::2])
        restored = _script_payloads(s.content for row in refined.lines for s in row.spans if s.kind == "text")
        orig_rel = _relation_counts(s.content for s in line.spans if s.kind == "text")
        clean_rel = _relation_counts(cleaned[::2])
        restored_rel = _relation_counts(s.content for row in refined.lines for s in row.spans if s.kind == "text")
        relation_missed = any(
            clean_rel[op] > orig_rel[op] and restored_rel[op] < clean_rel[op] for op in _RELATION_CHARS
        )
        if line_chars and (total != found or scripts != restored or relation_missed):
            lines.append(_clamp_line_formulas(line, chars, block_centers))
            continue
        originals = [s for s in line.spans if s.kind == "inline_formula" and s.content.strip()]
        index = 0
        for new_line in refined.lines:
            for j, span in enumerate(new_line.spans):
                if span.kind == "inline_formula":
                    if index in unchanged:
                        new_line.spans[j] = originals[unchanged[index]]
                    index += 1
        lines.extend(_clamp_line_formulas(row, chars, block_centers) for row in refined.lines)
    return block.model_copy(update={"lines": lines})


_SCRIPTS = re.compile(r"<(sup|sub)>([^<>]+)</\1>")


def _script_payloads(parts: Iterable[str]) -> list[tuple[str, str]]:
    return [(match[1], "".join(match[2].split())) for part in parts for match in _SCRIPTS.finditer(part)]


def _tagged_chars(
    content: str,
    chars: list[Char],
    owner: dict[int, int],
    rows: dict[int, int],
    *,
    trailing_after: str = "",
) -> dict[int, str]:
    """用清洗后的正文锚定上下标字形，再将标签写回重建的文字片段。"""
    plain = ""
    scripts: list[tuple[int, int, str]] = []
    for part in _content_parts(content, trailing_after=trailing_after)[::2]:
        text = re.sub(r"</?[bi]>", "", part)
        start = 0
        for match in _SCRIPTS.finditer(text):
            plain += "".join(text[start : match.start()].split())
            payload = "".join(match[2].split())
            scripts.append((len(plain), len(plain) + len(payload), match[1]))
            plain += payload
            start = match.end()
        plain += "".join(text[start:].split())
    if not scripts:
        return {}
    indices = [i for i, c in enumerate(chars) if i not in owner and not c.c.isspace()]
    actual = "".join(chars[k].c for k in indices)
    mapping = {
        a + i: indices[b + i]
        for a, b, count in difflib.SequenceMatcher(None, plain, actual, autojunk=False).get_matching_blocks()
        for i in range(count)
    }
    tagged: dict[int, str] = {}
    for lo, hi, tag in scripts:
        mapped = [mapping.get(i) for i in range(lo, hi)]
        if not mapped or any(i is None for i in mapped):
            continue
        glyphs = [k for k in mapped if k is not None]
        first, last = glyphs[0], glyphs[-1]
        if rows[id(chars[first])] != rows[id(chars[last])] or any(k in owner for k in range(first, last)):
            continue
        tagged[first] = f"<{tag}>" + chars[first].c
        tagged[last] = tagged.get(last, chars[last].c) + f"</{tag}>"
    return tagged


def refine_block(
    block: LayoutBlock,
    chars: Sequence[Char],
    *,
    block_centers: Sequence[float] | None = None,
    trailing_after: str = "",
) -> tuple[LayoutBlock, int, int]:
    """块级结果 → 带行与片段的块。返回（新块, 公式总数, 找到位置的公式数）。"""
    content = block.content or ""
    visible = [c for c in chars if not c.c.isspace()]
    if not visible:  # 扫描页等没有可用于定位的字符，仍按顺序保留公式与正文。
        parts = _content_parts(content, trailing_after=trailing_after)
        spans = [
            LayoutSpan(
                kind="inline_formula" if i % 2 else "text",
                content=part.strip() if i % 2 else part,
                bbox=block.bbox,
                located=False,
            )
            for i, part in enumerate(parts)
            if part.strip()
        ]
        return (
            block.model_copy(update={"lines": [LayoutLine(bbox=block.bbox, spans=spans)]}),
            sum(span.kind == "inline_formula" for span in spans),
            0,
        )

    rows = visual_lines(chars)
    centers = [statistics.median(c.bbox.center[1] for c in row) for row in rows]
    ordered = [c for row in rows for c in row]
    row_of = {id(c): n for n, row in enumerate(rows) for c in row}
    located = locate_formulas(content, ordered, [row_of[id(c)] for c in ordered], trailing_after=trailing_after)
    owner = {k: n for n, f in enumerate(located) for k in f.chars}  # PDF 字符下标 → 公式编号
    tagged = (
        _tagged_chars(content, ordered, owner, row_of, trailing_after=trailing_after)
        if _SCRIPTS.search(content) or ";" in content
        else {}
    )
    use_block_centers = block_centers is not None and len(block_centers) >= 2

    lines: list[LayoutLine] = []
    emitted: set[int] = set()
    for line_no in range(len(rows)):
        idx = [k for k, c in enumerate(ordered) if row_of[id(c)] == line_no]
        spans: list[LayoutSpan] = []
        run: list[Char] = []
        for k in idx:
            n = owner.get(k)
            if n is None:
                run.append(ordered[k].model_copy(update={"c": tagged[k]}) if k in tagged else ordered[k])
                continue
            _flush_text(run, spans)
            if n not in emitted:  # 跨行的公式只在第一行出一个片段，框取它在这一行的部分
                # 片段只放在首行，但裁剪框必须覆盖公式在后续视觉行中的字符，
                # 否则分式、矩阵等上下延伸公式会在导出时被截掉。
                part = [ordered[j] for j in located[n].chars]
                last_row = row_of[id(part[-1])]
                if use_block_centers and block_centers is not None:
                    first_idx = _nearest_center(block_centers, centers[line_no])
                    last_idx = _nearest_center(block_centers, centers[last_row])
                    bbox = _formula_box(part, block_centers, min(first_idx, last_idx), max(first_idx, last_idx))
                else:
                    bbox = _formula_box(part, centers, line_no, last_row)
                spans.append(LayoutSpan(kind="inline_formula", content=located[n].latex, bbox=bbox))
                emitted.add(n)
        _flush_text(run, spans)
        line_chars = [ordered[k] for k in idx if not ordered[k].c.isspace()]
        if spans and line_chars:
            lines.append(LayoutLine(bbox=_box(line_chars), spans=spans))
    found = sum(1 for f in located if f.chars)
    return block.model_copy(update={"lines": lines}), len(located), found
