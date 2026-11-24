"""以 PDF 文字层纠正能独立验证的解析错误。"""

import difflib
import re
import statistics
import unicodedata
from collections.abc import Sequence

from pdftr.domain.chars import Char
from pdftr.domain.geometry import Rect
from pdftr.domain.text import RICH_TAG, is_bad_char

MIN_SIMILARITY = 0.85
_SCRIPT = re.compile(r"<(sub|sup)>([^<>]+)</\1>")
_LETTER = re.compile(r"[A-Za-z]")


def script_glyph(ch: Char, size: float, baseline: float | None) -> bool:
    return ch.size <= 0.88 * size or (
        baseline is not None and ch.origin_y is not None and abs(ch.origin_y - baseline) >= 0.15 * size
    )


def main_metrics(chars: Sequence[Char]) -> tuple[float, float | None]:
    sizes = sorted(c.size for c in chars)
    med = statistics.median(sizes)
    top = sizes[-1]
    if sizes[0] <= 0.88 * top and top <= 1.75 * sizes[0]:
        major = [s for s in sizes if s > 0.88 * top]
        size = statistics.median(major) if len(sizes) <= 12 or len(major) >= max(2, int(len(sizes) * 0.15)) else med
    else:
        size = med
    bases = [c.origin_y for c in chars if c.origin_y is not None and 0.88 * size < c.size <= 1.14 * size]
    return size, statistics.median(bases) if bases else None


def chars_in(chars: Sequence[Char], box: Rect, exclude: Sequence[Rect] = (), tol: float = 1.0) -> list[Char]:
    """框内、且不在行内公式框里的字符。"""
    return [
        ch
        for ch in chars
        if box.contains_point(*ch.bbox.center, tol) and not any(e.contains_point(*ch.bbox.center, 0.5) for e in exclude)
    ]


def text_of(chars: Sequence[Char]) -> str:
    """按 PyMuPDF 行、横坐标排序后拼接；字符间距明显时补空格。"""
    ordered = sorted(chars, key=lambda c: (c.line, c.bbox.x0))
    parts: list[str] = []
    prev: Char | None = None
    for ch in ordered:
        if prev is not None:
            new_line = ch.line != prev.line
            gap = ch.bbox.x0 - prev.bbox.x1
            if (new_line or gap > 0.25 * ch.size) and parts and not parts[-1].endswith(" ") and ch.c != " ":
                parts.append(" ")
        parts.append(ch.c)
        prev = ch
    text = unicodedata.normalize("NFKC", "".join(parts))  # 连字 ﬁ → fi 等
    return re.sub(r"\s+", " ", text).strip()


def is_broken(text: str) -> bool:
    return any(_bad(c) for c in text)


def _bad(c: str) -> bool:
    return is_bad_char(c) or (c not in "\t\n\r" and unicodedata.category(c) in {"Cc", "Cf"})


def _repair_chars(text: str) -> str:
    plain = unicodedata.normalize("NFKC", RICH_TAG.sub("", text))
    return "".join(c for c in plain if not c.isspace() or _bad(c))


def _visible(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


_SPECIAL_SCRIPT = re.compile(r"[*∗†‡§¶®™]+")


def _glyph_script_flags(chars: Sequence[Char]) -> list[bool]:
    """按视觉行分别判定每个非空字符是否为真实上下标，避免多行 span 或容差蹭行把行距误判成基线偏移。"""
    by_line: dict[int, list[int]] = {}
    for idx, ch in enumerate(chars):
        by_line.setdefault(ch.line, []).append(idx)
    # 合并属于同一视觉行的 PyMuPDF 行（上下标常被 PyMuPDF 拆成单独 line）
    groups: list[list[int]] = []
    line_items = sorted(by_line.values(), key=lambda ids: statistics.median(chars[i].bbox.center[1] for i in ids))
    for ids in line_items:
        sz = statistics.median(chars[i].size for i in ids)
        cy = statistics.median(chars[i].bbox.center[1] for i in ids)
        for g in reversed(groups):
            g_sz = statistics.median(chars[i].size for i in g)
            g_cy = statistics.median(chars[i].bbox.center[1] for i in g)
            if abs(cy - g_cy) < 0.6 * max(sz, g_sz):
                g.extend(ids)
                break
        else:
            groups.append(list(ids))
    flags = [False] * len(chars)
    for g in groups:
        row_chars = [chars[i] for i in g]
        size, baseline = main_metrics(row_chars)
        for i in g:
            flags[i] = script_glyph(chars[i], size, baseline)
    return flags


def strip_fake_scripts(parser_text: str, span_chars: Sequence[Char]) -> str:
    """用字号和基线区分正文与真实脚本，缺少字形证据时只清理明显的词内标签。"""
    if not _SCRIPT.search(parser_text):
        return parser_text
    chars = sorted((c for c in span_chars if not c.c.isspace()), key=lambda c: (c.line, c.bbox.x0))
    char_flags = _glyph_script_flags(chars) if chars else []
    glyph_flags = [flag for ch, flag in zip(chars, char_flags, strict=True) for _ in _visible(ch.c)]
    has_scripts = any(glyph_flags)
    plain = _visible(RICH_TAG.sub("", parser_text))
    pdf = "".join(_visible(c.c) for c in chars)
    mapping: dict[int, bool] = {}
    if has_scripts:
        for a, b, count in difflib.SequenceMatcher(None, plain, pdf, autojunk=False).get_matching_blocks():
            mapping.update((a + i, glyph_flags[b + i]) for i in range(count))

    def replace(match: re.Match[str]) -> str:
        payload = match[2]
        if _SPECIAL_SCRIPT.fullmatch(payload.strip()):
            return match[0]
        if chars:
            if not has_scripts:
                return payload
            payload_vis = _visible(payload)
            if not payload_vis:
                return match[0]
            start = len(_visible(RICH_TAG.sub("", parser_text[: match.start(2)])))
            for i in range(start, start + len(payload_vis)):
                if (is_script := mapping.get(i)) is None or is_script:
                    return match[0]
            return payload
        # 扫描页等缺少字形证据时，仅保守清理纯分隔标点和被字母夹在词中的单字母假标。
        if re.fullmatch(r"[,;]+", payload):
            return payload
        left = re.search(r"[A-Za-z]+$", RICH_TAG.sub("", parser_text[: match.start()]))
        right = re.match(r"[A-Za-z]+", RICH_TAG.sub("", parser_text[match.end() :]))
        if (
            left is not None
            and right is not None
            and re.fullmatch(r"[A-Za-z]", payload) is not None
            and len(left[0]) + 1 + len(right[0]) >= 5
        ):
            return payload
        return match[0]

    return _SCRIPT.sub(replace, parser_text)


_SAFE_INSERT_SYMBOLS = frozenset("×−_{}[]()")
_WORD_EDGE_PUNCT = "\"'“”‘’()[]{}.,;:!?"
_SINGLE_LETTER_WORDS = frozenset({"a", "A", "I"})
_CITE_SUP_CANDIDATE = re.compile(
    r"(?:(?<![\d])(?P<punct>[.,;:!?)\]\}”’\"'])|(?P<word>[A-Za-z]?[a-z]{2,}))"
    r"(?P<nums>\d{1,3}(?:[,–—\-−]\d{1,3})*)"
    r"(?=$|[\s.,;:!?)\]\}”’\"']|</(?:b|i|sup|sub)>)"
)
_NO_CITE_DOT_PREFIX = re.compile(
    r"(?:^|[\s(\[])(?:fig|figs|eq|eqs|sec|secs|no|nos|vol|vs|ref|refs|tab|art|ch|p|pp|v)$", re.I
)


def _strip_edge_punct(token: str) -> str:
    return token.strip(_WORD_EDGE_PUNCT)


def _valid_split_subword(word: str, *, is_first: bool) -> bool:
    core = _strip_edge_punct(word)
    if not core or not core.isalpha():
        return False
    if len(core) == 1:
        return core in _SINGLE_LETTER_WORDS
    return is_first or core[0].islower()


def _recover_glued_spaces(parser_text: str, pdf_text: str) -> str:
    """字符完全一致时，仅把 parser 粘连在一起、在 PDF 中明确分开的合法英文词拆开。"""
    plain = RICH_TAG.sub("", parser_text)
    p_words = plain.split()
    f_words = pdf_text.split()
    if p_words == f_words:
        return parser_text
    rebuilt: list[str] = []
    pi = fi = 0
    changed = False
    while pi < len(p_words) and fi < len(f_words):
        pw, fw = p_words[pi], f_words[fi]
        if pw == fw:
            rebuilt.append(pw)
            pi += 1
            fi += 1
            continue
        if len(pw) > len(fw) and pw.startswith(fw):
            chunk: list[str] = []
            acc = ""
            fj = fi
            while fj < len(f_words) and len(acc) < len(pw):
                chunk.append(f_words[fj])
                acc += f_words[fj]
                fj += 1
            if acc == pw and len(chunk) >= 2:
                stripped = _strip_edge_punct(pw)
                if stripped.isalpha() and all(
                    _valid_split_subword(sub, is_first=idx == 0) for idx, sub in enumerate(chunk)
                ):
                    rebuilt.extend(chunk)
                    changed = True
                else:
                    rebuilt.append(pw)
                pi += 1
                fi = fj
                continue
        # 同步前进到下一个词边界重合点，保留 parser 原本的分词，绝不把 parser 已分开的词粘起来。
        p_acc, f_acc = pw, fw
        pj, fj = pi + 1, fi + 1
        while p_acc != f_acc:
            if len(p_acc) < len(f_acc):
                if pj >= len(p_words):
                    return parser_text
                p_acc += p_words[pj]
                pj += 1
            else:
                if fj >= len(f_words):
                    return parser_text
                f_acc += f_words[fj]
                fj += 1
        rebuilt.extend(p_words[pi:pj])
        pi, fi = pj, fj
    if pi != len(p_words) or fi != len(f_words) or not changed:
        return parser_text
    return _restore_tags(parser_text, " ".join(rebuilt))


def _has_bad_kerning_split(plain: str, pdf_text: str) -> bool:
    """拒绝 PDF 因字距微调把完整英文词拆出的单字母碎片（如 'f rom'）。"""
    parser_tokens = set(plain.split())
    for word in pdf_text.split():
        core = _strip_edge_punct(word)
        if len(core) == 1 and core.isalpha() and core not in _SINGLE_LETTER_WORDS and word not in parser_tokens:
            return True
    return False


def _valid_inserted_digit(word: str, word_matched: list[bool], idx: int) -> bool:
    """只允许恢复括号内紧贴的短数字（如 getpeername(2).、foo[1]）。"""
    lo = idx
    while lo > 0 and word[lo - 1].isdigit():
        lo -= 1
    hi = idx + 1
    while hi < len(word) and word[hi].isdigit():
        hi += 1
    if hi - lo > 3 or lo == 0 or hi >= len(word):
        return False
    open_ch, close_ch = word[lo - 1], word[hi]
    if (open_ch, close_ch) not in {("(", ")"), ("[", "]")}:
        return False
    return sum(word_matched) >= 2


def recover_dropped_chars(parser_text: str, pdf_text: str) -> str:
    """补上已有词内/词尾少量缺字、受控数学/代码符号与括号、以及被粘连词的真实词间空格。"""
    if re.search(r"</?(?:sub|sup)>", parser_text) or not parser_text.strip() or is_broken(pdf_text):
        return parser_text
    plain = RICH_TAG.sub("", parser_text)
    wanted = "".join(plain.split())
    actual = "".join(pdf_text.split())
    missing = len(actual) - len(wanted)
    if missing == 0:
        return _recover_glued_spaces(parser_text, pdf_text) if wanted == actual else parser_text
    if not 0 < missing <= min(16, max(4, len(wanted) // 3)):
        return parser_text

    # wanted 必须是 actual 的严格子序列，新增字符须为英文字母、尾标点、受控安全符号或括号内短数字。
    matched = [False] * len(actual)
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, wanted, actual, autojunk=False).get_opcodes():
        if tag == "equal":
            for j in range(j1, j2):
                matched[j] = True
        elif tag == "insert":
            for j in range(j1, j2):
                ch = actual[j]
                if not (_LETTER.fullmatch(ch) or ch in ",." or ch in _SAFE_INSERT_SYMBOLS or ch.isdigit()):
                    return parser_text
        else:
            return parser_text

    if _has_bad_kerning_split(plain, pdf_text):
        return parser_text

    # 按 PDF 词边界检查：每个词都必须已有匹配字符（允许独立乘号 '×' 夹在数字表达式间），并逐类校验新增字符上下文。
    pdf_words = pdf_text.split()
    pos = 0
    for wi, word in enumerate(pdf_words):
        end = pos + len(word)
        word_matched = matched[pos:end]
        pos = end
        if not any(word_matched):
            if (
                word == "×"
                and 0 < wi < len(pdf_words) - 1
                and pdf_words[wi - 1][-1].isdigit()
                and pdf_words[wi + 1][0].isalnum()
            ):
                continue
            return parser_text
        word_missing = word_matched.count(False)
        if not word_missing:
            continue
        letter_missing = 0
        for idx, (ch, ok) in enumerate(zip(word, word_matched, strict=True)):
            if ok:
                continue
            if ch in ",.":
                # 新增的逗号/句号只允许出现在词尾（可紧跟闭合括号/标点），禁止在词内插入标点。
                tail = word[idx + 1 :]
                if tail and any(c.isalnum() for c in tail):
                    return parser_text
            elif ch == "×":
                between = 0 < idx < len(word) - 1 and (
                    (word[idx - 1].isdigit() and word[idx + 1].isalnum())
                    or (word[idx - 1].isalpha() and word[idx + 1].isdigit())
                )
                trailing = idx == len(word) - 1 and idx > 0 and word[idx - 1].isdigit()
                if not (between or trailing):
                    return parser_text
            elif ch == "−":
                between = 0 < idx < len(word) - 1 and word[idx - 1].isalnum() and word[idx + 1].isalnum()
                neg_num = idx + 1 < len(word) and word[idx + 1].isdigit() and (idx == 0 or word[idx - 1] in "([{")
                if not (between or neg_num):
                    return parser_text
            elif ch == "_":
                between = 0 < idx < len(word) - 1 and word[idx - 1].isalnum() and word[idx + 1].isalnum()
                trailing = idx == len(word) - 1 and idx >= 2 and word[idx - 1].isalnum()
                if not (between or trailing):
                    return parser_text
            elif ch in "{}[]()":
                # 括号允许位于词首/词尾/括号或数字边界，禁止将普通英文字母单词从中间劈开。
                if 0 < idx < len(word) - 1 and word[idx - 1].isalpha() and word[idx + 1].isalpha():
                    return parser_text
            elif ch.isdigit():
                if not _valid_inserted_digit(word, word_matched, idx):
                    return parser_text
            elif _LETTER.fullmatch(ch):
                letter_missing += 1
            else:
                return parser_text
        if letter_missing:
            letters = sum(bool(_LETTER.fullmatch(ch)) for ch in word)
            matched_letters = sum(bool(_LETTER.fullmatch(ch)) and ok for ch, ok in zip(word, word_matched, strict=True))
            if letters < 3 or matched_letters < 1 or letter_missing > max(2, letters // 2):
                return parser_text
    return _restore_tags(parser_text, pdf_text)


def _glyph_superscript_flags(chars: Sequence[Char]) -> tuple[list[bool], list[int]]:
    """按视觉行判定每个非空字符是否为严格上标（字号显著缩小且基线上抬），并返回行分组号。"""
    by_line: dict[int, list[int]] = {}
    for idx, ch in enumerate(chars):
        by_line.setdefault(ch.line, []).append(idx)
    groups: list[list[int]] = []
    line_items = sorted(by_line.values(), key=lambda ids: statistics.median(chars[i].bbox.center[1] for i in ids))
    for ids in line_items:
        sz = statistics.median(chars[i].size for i in ids)
        cy = statistics.median(chars[i].bbox.center[1] for i in ids)
        for g in reversed(groups):
            g_sz = statistics.median(chars[i].size for i in g)
            g_cy = statistics.median(chars[i].bbox.center[1] for i in g)
            if abs(cy - g_cy) < 0.6 * max(sz, g_sz):
                g.extend(ids)
                break
        else:
            groups.append(list(ids))
    is_sup = [False] * len(chars)
    row_id = [0] * len(chars)
    for rno, g in enumerate(groups):
        row_chars = [chars[i] for i in g]
        size, baseline = main_metrics(row_chars)
        normal_cys = [c.bbox.center[1] for c in row_chars if 0.88 * size < c.size <= 1.14 * size]
        main_cy = (
            statistics.median(normal_cys) if normal_cys else statistics.median(c.bbox.center[1] for c in row_chars)
        )
        for i in g:
            row_id[i] = rno
            ch = chars[i]
            if not script_glyph(ch, size, baseline) or ch.size > 0.88 * size:
                continue
            if baseline is not None and ch.origin_y is not None:
                is_sup[i] = baseline - ch.origin_y >= 0.15 * size
            else:
                is_sup[i] = main_cy - ch.bbox.center[1] >= 0.12 * size
    return is_sup, row_id


def restore_citation_superscripts(parser_text: str, span_chars: Sequence[Char]) -> str:
    """原生页补回被解析器漏标 <sup> 的词尾/标点后上标数字引文（如 studies.11–13 → studies.<sup>11–13</sup>）。"""
    if not parser_text.strip() or not span_chars or not re.search(r"\d", parser_text):
        return parser_text
    chars = sorted((c for c in span_chars if not c.c.isspace()), key=lambda c: (c.line, c.bbox.x0))
    if not chars:
        return parser_text
    char_sup, char_row = _glyph_superscript_flags(chars)
    if not any(char_sup):
        return parser_text
    glyph_sup = [flag for ch, flag in zip(chars, char_sup, strict=True) for _ in _visible(ch.c)]
    glyph_row = [rno for ch, rno in zip(chars, char_row, strict=True) for _ in _visible(ch.c)]
    plain = _visible(RICH_TAG.sub("", parser_text))
    pdf = "".join(_visible(c.c) for c in chars)
    mapping: dict[int, int] = {}
    for a, b, count in difflib.SequenceMatcher(None, plain, pdf, autojunk=False).get_matching_blocks():
        for i in range(count):
            mapping[a + i] = b + i

    # 记录现有 <sup>/<sub> 覆盖区间，避免嵌套标签。
    script_spans: list[tuple[int, int]] = [(m.start(), m.end()) for m in _SCRIPT.finditer(parser_text)]

    def in_script(start: int, end: int) -> bool:
        return any(lo < end and start < hi for lo, hi in script_spans)

    replacements: list[tuple[int, int, str]] = []
    for match in _CITE_SUP_CANDIDATE.finditer(parser_text):
        lo, hi = match.start("nums"), match.end("nums")
        if in_script(lo, hi):
            continue
        if match.group("punct") == ".":
            prefix_plain = RICH_TAG.sub("", parser_text[: match.start("punct")])
            if _NO_CITE_DOT_PREFIX.search(prefix_plain):
                continue
        nums = match.group("nums")
        nums_vis = _visible(nums)
        if not nums_vis:
            continue
        vis_start = len(_visible(RICH_TAG.sub("", parser_text[:lo])))
        mapped = [mapping.get(i) for i in range(vis_start, vis_start + len(nums_vis))]
        if any(idx is None for idx in mapped):
            continue
        glyph_idxs = [idx for idx in mapped if idx is not None]
        # 紧邻前导字符本身若也是上标（例如复杂上标片段的一部分），避免切碎。
        if vis_start > 0 and (prev_idx := mapping.get(vis_start - 1)) is not None and glyph_sup[prev_idx]:
            continue
        rows = {glyph_row[idx] for idx in glyph_idxs}
        if len(rows) != 1:
            continue
        # 所有数字字符必须严格满足上标几何，标点连接符（–、,、-）也须处于上标或同行小字。
        if not all(glyph_sup[idx] for ch, idx in zip(nums_vis, glyph_idxs, strict=True) if ch.isdigit()):
            continue
        replacements.append((lo, hi, f"<sup>{nums}</sup>"))
    if not replacements:
        return parser_text
    out = parser_text
    for lo, hi, rep in reversed(replacements):
        out = out[:lo] + rep + out[hi:]
    return out


def _restore_tags(original: str, replacement: str) -> str:
    """把已对齐的富文本边界映射回替换文本，含脚本的内容必须完整对应或原本即坏字。"""
    if not RICH_TAG.search(original):
        return replacement
    plain = RICH_TAG.sub("", original)
    matcher = difflib.SequenceMatcher(None, plain, replacement, autojunk=False)
    old_to_new: dict[int, set[int]] = {}
    unchanged: set[int] = set()
    for a, b, count in matcher.get_matching_blocks():
        if count:
            unchanged.update(range(a, a + count))
            for i in range(count + 1):
                old_to_new.setdefault(a + i, set()).add(b + i)
    script_start: int | None = None
    script_new_start: int | None = None
    tags: list[tuple[int, str]] = []
    offset = 0
    for tag in RICH_TAG.finditer(original):
        pos = tag.start() - offset
        offset += len(tag[0])
        targets = old_to_new.get(pos, set())
        if len(targets) != 1:
            return original
        mapped = next(iter(targets))
        if tag[0] in {"<sub>", "<sup>"}:
            script_start, script_new_start = pos, mapped
        elif tag[0] in {"</sub>", "</sup>"}:
            if script_start is None or script_new_start is None:
                return original
            span_orig = plain[script_start:pos]
            span_new = replacement[script_new_start:mapped]
            repaired_bad = any(_bad(c) for c in span_orig) and bool(span_new) and not any(c.isspace() for c in span_new)
            if not (all(i in unchanged for i in range(script_start, pos)) or repaired_bad):
                return original
            script_start = script_new_start = None
        tags.append((mapped, tag[0]))
    for pos, tag in reversed(tags):
        replacement = replacement[:pos] + tag + replacement[pos:]
    return replacement


def choose(parser_text: str, pdf_text: str) -> tuple[str, bool]:
    """返回 (采用的文字, 是否采用了 PDF 文字)。"""
    if not parser_text.strip():
        return (pdf_text, True) if pdf_text and not is_broken(pdf_text) else (parser_text, False)
    parser = _repair_chars(parser_text)
    pdf = _repair_chars(pdf_text)
    bad = sum(_bad(c) for c in parser)
    pdf_bad = sum(_bad(c) for c in pdf)
    if not pdf or pdf_bad >= bad:
        return parser_text, False
    a = "".join(c for c in parser if not _bad(c)).casefold()
    b = "".join(c for c in pdf if not _bad(c)).casefold()
    if not a:
        safe = bad <= 4 and pdf_bad == 0 and len(pdf) == bad
    else:
        matches = sum(m.size for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
        safe = matches >= MIN_SIMILARITY * len(a) and abs(len(pdf) - len(parser)) <= max(1, len(parser) // 4)
    if not safe:
        return parser_text, False
    text = _restore_tags(parser_text, pdf_text)
    if text == parser_text and pdf_bad == 0 and not re.search(r"</?[bi]>", parser_text):
        scripts = list(_SCRIPT.finditer(parser_text))
        if scripts and all(any(_bad(c) for c in m[2]) for m in scripts):
            return pdf_text, True
    return text, text != parser_text
