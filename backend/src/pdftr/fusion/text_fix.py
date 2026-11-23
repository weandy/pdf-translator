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


def recover_dropped_chars(parser_text: str, pdf_text: str) -> str:
    """仅补上已有英文词内部、词首或词尾的少量缺字，拒绝整词外溢与非字母差异。"""
    if re.search(r"</?(?:sub|sup)>", parser_text) or not parser_text.strip() or is_broken(pdf_text):
        return parser_text
    plain = RICH_TAG.sub("", parser_text)
    wanted = "".join(plain.split())
    actual = "".join(pdf_text.split())
    missing = len(actual) - len(wanted)
    if not 0 < missing <= min(16, max(2, len(wanted) // 4)):
        return parser_text

    # wanted 必须是 actual 的严格子序列，且所有新增字符必须为纯英文字母。
    matched = [False] * len(actual)
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, wanted, actual, autojunk=False).get_opcodes():
        if tag == "equal":
            for j in range(j1, j2):
                matched[j] = True
        elif tag == "insert":
            if any(not _LETTER.fullmatch(actual[j]) for j in range(j1, j2)):
                return parser_text
        else:
            return parser_text

    # 按 PDF 词边界检查：每个词都必须已有匹配字符（禁止行首/行尾整词外溢），且补字限于词内合理比例。
    pos = 0
    for word in pdf_text.split():
        end = pos + len(word)
        word_matched = matched[pos:end]
        pos = end
        if not any(word_matched):
            return parser_text
        word_missing = word_matched.count(False)
        if not word_missing:
            continue
        letters = sum(bool(_LETTER.fullmatch(ch)) for ch in word)
        matched_letters = sum(bool(_LETTER.fullmatch(ch)) and ok for ch, ok in zip(word, word_matched, strict=True))
        if letters < 3 or matched_letters < 1 or word_missing > max(2, letters // 2):
            return parser_text
    return _restore_tags(parser_text, pdf_text)


def _restore_tags(original: str, replacement: str) -> str:
    """把已对齐的富文本边界映射回替换文本，含脚本的内容必须完整对应。"""
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
    tags: list[tuple[int, str]] = []
    offset = 0
    for tag in RICH_TAG.finditer(original):
        pos = tag.start() - offset
        offset += len(tag[0])
        targets = old_to_new.get(pos, set())
        if len(targets) != 1:
            return original
        if tag[0] in {"<sub>", "<sup>"}:
            script_start = pos
        elif tag[0] in {"</sub>", "</sup>"}:
            if script_start is None or not all(i in unchanged for i in range(script_start, pos)):
                return original
            script_start = None
        tags.append((next(iter(targets)), tag[0]))
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
    return text, text != parser_text
