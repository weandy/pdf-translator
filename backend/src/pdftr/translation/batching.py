"""分批与结果校验。

S7：约 1500 token 一批，比逐段请求或小批次省约 64% 输入；“编号|原文”紧凑格式，只要求输出译文。
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from pdftr.domain.ir import BlockType
from pdftr.domain.text import RICH_TAG

BATCH_TOKENS = 1500
VALIDATION_VERSION = 8
_PLACEHOLDER = re.compile(r"<m\d+/>")
_SPACED_FORMULA_PLACEHOLDER = re.compile(r"<(m\d+)\s+/>")
_RICH_TAG = re.compile(r"<(/?)(b|i|sup|sub)>")


def normalize_formula_placeholders(text: str) -> str:
    """仅将无属性纯空格自闭合形式 <m1 /> 规范化为 <m1/>，不放行带属性或未闭合的非法标签。"""
    return _SPACED_FORMULA_PLACEHOLDER.sub(r"<\1/>", text)


_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
_WESTERN = r"(?:[A-Za-z0-9]|<m\d+/>)"
_FORMAT_TAGS = r"(?:</?(?:b|i|sup|sub)>)*"
_MIXED_SPACE = re.compile(
    rf"({_HAN.pattern}{_FORMAT_TAGS})[ \t\u00a0](?={_FORMAT_TAGS}{_WESTERN})"
    rf"|({_WESTERN}{_FORMAT_TAGS})[ \t\u00a0](?={_FORMAT_TAGS}{_HAN.pattern})"
)
_LEADING_LABEL = re.compile(
    rf"^([ \t\u00a0]*{_FORMAT_TAGS}"
    rf"(?:(?:图|表|公式|算法|定理|引理|命题|推论|定义|注){_FORMAT_TAGS}[ \t\u00a0]*{_FORMAT_TAGS}[A-Z]?\.?\d+(?:\.\d+)*"
    rf"|[1-9]\d?(?:\.\d+)*"
    rf"|[A-Z](?:\.\d+)+"
    rf"|[A-Z](?={_FORMAT_TAGS}[ \t\u00a0]+{_FORMAT_TAGS}附录))"
    rf"{_FORMAT_TAGS})([ \t\u00a0])(?={_FORMAT_TAGS}{_HAN.pattern})"
)


def _strip_mixed_space(text: str) -> str:
    return _MIXED_SPACE.sub(lambda match: match.group(1) or match.group(2), text)


def normalize_cjk_spacing(text: str) -> str:
    """移除汉字与西文、数字、公式间的单个横向空格，保留富文本标签与行首章节/图表编号分隔空格。"""
    if not text:
        return text
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if m := _LEADING_LABEL.match(line):
            out.append(f"{_strip_mixed_space(m.group(1))}{m.group(2)}{_strip_mixed_space(line[m.end() :])}")
        else:
            out.append(_strip_mixed_space(line))
    return "".join(out)


def estimate_tokens(text: str) -> int:
    """粗估 token：英文约 4 字符 1 token，中日韩字符约 1 字 1 token。够用于分批和费用预估。"""
    cjk = len(re.findall(r"[\u3000-\u9fff\uff00-\uffef]", text))
    return cjk + (len(text) - cjk) // 4 + 1


def make_batches[T](
    items: Sequence[T],
    sizes: Sequence[int],
    limit: int = BATCH_TOKENS,
    *,
    sections: Sequence[str] | None = None,
    roles: Sequence[BlockType | None] | None = None,
) -> list[list[T]]:
    batches: list[list[T]] = []
    cur: list[T] = []
    total = 0
    cur_section = ""
    for item, size, section, role in zip(
        items,
        sizes,
        sections if sections is not None else ("",) * len(items),
        roles if roles is not None else (None,) * len(items),
        strict=True,
    ):
        new_section = role is BlockType.TITLE or (cur_section and section and section != cur_section)
        if cur and (total + size > limit or new_section):
            batches.append(cur)
            cur, total = [], 0
            cur_section = ""
        cur.append(item)
        total += size
        cur_section = section or cur_section
    if cur:
        batches.append(cur)
    return batches


class NumberedResponseError(ValueError):
    """模型返回的编号无法可靠地匹配输入。"""


_NUMBERED = re.compile(r"^(\d+)\s*[|｜]\s*(.*)$")
_PIPE_CONTINUATION = re.compile(r"^\d+\s+[|｜]\s+")
_START_TRANSLATIONS = re.compile(r"^<translations\s*>\s*", re.I)
# 只消费完整的响应边界，段内残缺标签交给译文校验重试。
_END_TRANSLATIONS = re.compile(r"</translations\s*>", re.I)


def parse_numbered(text: str, *, batch_size: int | None = None, sources: Sequence[str] | None = None) -> dict[int, str]:
    """解析「编号|译文」。没有编号的行视为上一行的续行（模型偶尔会在段内换行）。"""
    out: dict[int, str] = {}
    last: int | None = None
    for raw in text.splitlines():
        line = _START_TRANSLATIONS.sub("", raw.strip())
        end_match = _END_TRANSLATIONS.search(line)
        stop = end_match is not None
        if end_match is not None:
            line = line[: end_match.start()].strip()
        if not line or line.startswith("```"):
            if stop:
                break
            continue
        match = _NUMBERED.match(line)
        if match:
            digits = match[1]
            # 防止极长的数字触发 Python 的整数转换限制。
            if len(digits) > 12:
                raise NumberedResponseError("编号越界")
            number = int(digits)
            out_of_bounds = number < 1 or (batch_size is not None and number > batch_size)
            if not (
                last is not None
                and (number <= last or out_of_bounds)
                and _PIPE_CONTINUATION.match(line)
                and sources is not None
                and last <= len(sources)
                and ("|" in sources[last - 1] or "｜" in sources[last - 1])
            ):
                if out_of_bounds:
                    raise NumberedResponseError(f"编号 {number} 越界")
                if number in out:
                    raise NumberedResponseError(f"编号 {number} 重复")
                if last is not None and number < last:
                    raise NumberedResponseError(f"编号 {number} 乱序")
                last = number
                out[last] = match[2].strip()
                if stop:
                    break
                continue
        if last is not None:
            previous = out[last]
            space = " " if re.search(r"[A-Za-z0-9]$", previous) and re.match(r"[A-Za-z0-9]", line) else ""
            out[last] = f"{previous}{space}{line}"
        if stop:
            break
    return out


def placeholders_match(source: str, target: str) -> bool:
    return Counter(_PLACEHOLDER.findall(source)) == Counter(_PLACEHOLDER.findall(target))


def placeholder_problems(source: str, target: str) -> list[str]:
    """译文与原文的公式占位符不一致时，逐条说明（面向用户）；一致时返回空列表。"""
    want, got = Counter(_PLACEHOLDER.findall(source)), Counter(_PLACEHOLDER.findall(target))
    problems = []
    for tag in sorted(set(want) | set(got), key=lambda t: int(re.sub(r"\D", "", t) or 0)):
        n = tag[2:-2]  # <m2/> → 2
        if got[tag] < want[tag]:
            problems.append(f"少了公式 {n}（{tag}）")
        elif got[tag] > want[tag]:
            problems.append(f"公式 {n}（{tag}）出现了 {got[tag]} 次" if want[tag] else f"多了不存在的公式占位 {tag}")
    return problems


def _emphasis_problems(source: str, target: str, *, tag: str, label: str) -> list[str]:
    """上下标固定对数，局部粗斜体允许调整跨度。"""
    expected = source.count(f"<{tag}>")
    opened, closed = target.count(f"<{tag}>"), target.count(f"</{tag}>")
    if not expected:
        if opened or closed:
            if opened != closed:
                return [f"译文新增{label}标记且未完整配对"]
            return [f"译文新增{label}标记：原文没有{label}，实际 {opened} 对"]
        return []
    if opened != closed:
        return [f"{label}标记未完整配对"]
    script = tag in ("sup", "sub")
    required = expected if script else 1
    if opened < required:
        return [f"{label}标记丢失：至少需要 {required} 对，实际 {opened} 对"]
    if script and opened > expected:
        return [f"{label}标记多出：需要 {expected} 对，实际 {opened} 对"]
    if problems := rich_text_problems(target, tag=tag):
        return problems
    if not script:
        outside = _outside_emphasis(source, tag)
        words = re.findall(r"[A-Za-z]{2,}", _PLACEHOLDER.sub(" ", outside))
        outside_words = sum(w.lower() not in _EMPHASIS_STOP_WORDS for w in words) + len(_PLACEHOLDER.findall(outside))
        if outside_words >= 2 and not _EMPHASIS_PUNCTUATION.sub("", _outside_emphasis(target, tag)):
            return [f"{label}覆盖了整段正文"]
    return []


_TAG_LABELS = {"b": "粗体", "i": "斜体", "sup": "上标", "sub": "下标"}
_EMPHASIS_STOP_WORDS = {
    "a", "an", "the", "in", "on", "of", "to", "for", "and", "or", "is", "are", "was", "were", "be", "by", "at", "as",
}  # fmt: skip
_EMPHASIS_PUNCTUATION = re.compile(r"""[\s，。、；：？！（）()\[\]「」『』“”"'—–,.;:?!-]""")


def _outside_emphasis(text: str, tag: str) -> str:
    outside = []
    depth = cursor = 0
    for match in _RICH_TAG.finditer(text):
        if match[2] != tag:
            continue
        if not match[1]:
            if depth == 0:
                outside.append(text[cursor : match.start()])
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                cursor = match.end()
    if depth == 0:
        outside.append(text[cursor:])
    return " ".join(RICH_TAG.sub("", part) for part in outside)


def rich_text_problems(target: str, *, tag: str | None = None) -> list[str]:
    """人工编辑检查整个栈，模型验收也可检查某一种标签的结构。"""
    stack: list[tuple[str, int]] = []
    for match in _RICH_TAG.finditer(target):
        closing, current = match.group(1), match.group(2)
        if not closing:
            if tag == current and tag in ("b", "i") and any(t == tag for t, _ in stack):
                return [f"{_TAG_LABELS[current]}标记不能自我嵌套"]
            stack.append((current, match.end()))
            continue
        if stack and stack[-1][0] == current:
            _, start = stack.pop()
            if tag in (None, current) and not _RICH_TAG.sub("", target[start : match.start()]).strip():
                return [f"{_TAG_LABELS[current]}标记内没有内容"]
        elif tag in (None, current) or any(t == tag for t, _ in stack):
            label = _TAG_LABELS[tag or current]
            problem = "嵌套错误" if stack or f"<{current}>" in target else "未完整配对"
            return [f"{label}标记{problem}"]
    for current, _ in stack:
        if tag in (None, current):
            return [f"{_TAG_LABELS[current]}标记未完整配对"]
    return []


def bold_problems(source: str, target: str) -> list[str]:
    """校验局部粗体的拆合、配对及整段覆盖。"""
    return _emphasis_problems(source, target, tag="b", label="粗体")


def italic_problems(source: str, target: str) -> list[str]:
    """校验局部斜体的拆合、配对及整段覆盖。"""
    return _emphasis_problems(source, target, tag="i", label="斜体")


def script_problems(source: str, target: str) -> list[str]:
    return _emphasis_problems(source, target, tag="sup", label="上标") + _emphasis_problems(
        source, target, tag="sub", label="下标"
    )


_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_URL = r"(?i:https?://|www\.)\S+"
_URL_RE = re.compile(_URL)
_CITATION_BOUNDARY = re.compile(rf"{_URL}|{_PLACEHOLDER.pattern}")
_SOURCE_BRACKETS = re.compile(r"\[([0-9０-９\s,;\-–—]{1,60})\]")
_TARGET_BRACKETS = re.compile(r"\[([0-9０-９\s,;，、；\-–—]{1,60})\]|［([0-9０-９\s,;，、；\-–—]{1,60})］")
_LOCATOR_EN = (
    r"(?:Prop(?:osition)?|Thm|Theorem|Lem(?:ma)?|Cor(?:ollary)?|Def(?:inition)?|"
    r"Eq(?:uation)?s?|Sec(?:tion)?s?|Chap(?:ter)?s?|Ch|App(?:endix)?|Fig(?:ure)?s?|"
    r"Tab(?:le)?s?|Alg(?:orithm)?|Rem(?:ark)?|Ex(?:ample)?|Fact|Claim|Assumption|"
    r"Condition|Property|Problem|Part|Step|Case|Note|Footnote|Pages?|pp?)(?![A-Za-z])\.?|§"
)
_LOCATOR_ZH = (
    r"(?:命题|定理|引理|推论|定义|公式|方程|式|"
    r"第\s*[0-9０-９一二三四五六七八九十A-Za-z]+\s*[章节页步部分]|"
    r"节|章|附录|图|表|算法|注|例|事实|断言|假设|条件|性质|问题|脚注|页)"
)
_SOURCE_LOCATOR_BRACKET = re.compile(rf"\[(\s*[0-9０-９]{{1,4}})\s*[,;]\s*(?:{_LOCATOR_EN})[^\[\]]{{0,80}}\]", re.I)
_TARGET_LOCATOR_BRACKET = re.compile(
    rf"\[(\s*[0-9０-９]{{1,4}})\s*[,;，、；]\s*(?:{_LOCATOR_EN}|{_LOCATOR_ZH})[^\[\]［］]{{0,80}}\]|"
    rf"［(\s*[0-9０-９]{{1,4}})\s*[,;，、；]\s*(?:{_LOCATOR_EN}|{_LOCATOR_ZH})[^\[\]［］]{{0,80}}］",
    re.I,
)
_CITATION_SEPARATORS = re.compile(r"[,;，、；]")
_CITATION_ITEM = re.compile(r"\s*([0-9]{1,4})(?:\s*[-–—]\s*([0-9]{1,4}))?\s*")
_CITATION_PREFIX = re.compile(r"(?:(?<![A-Za-z])(?:Refs?\.|Citation)|参考文献|文献)$", re.I)
_INDEX_NAMES = {
    "arr", "vec", "mat", "seq", "idx", "dim", "buf", "mem", "ptr", "reg", "tab", "row", "col", "node", "edge", "tensor",
    "layer",
}  # fmt: skip
_DATA_NAMES = {
    "interval", "intervals", "range", "ranges", "vector", "vectors", "list", "lists", "array", "arrays",
    "matrix", "matrices", "tuple", "tuples", "shape", "shapes", "size", "sizes", "dim", "dims",
    "dimension", "dimensions", "kernel", "kernels", "stride", "strides",
}  # fmt: skip
_MATH_IN_SUBJECTS = {"value", "values", "lie", "lies", "lying", "fall", "falls", "falling", "sampled", "uniform"}
_DATA_NAMES_ZH = ("区间", "范围", "数组", "向量", "矩阵")
_ZH_INTERVAL_PREFIX = ("在", "位于", "落于", "落在", "处于", "属于")
_ZH_INTERVAL_SUFFIX = re.compile(r"^(?:之?内|之间|区间|范围|中(?:随机|均匀|采样|抽样|取值|选取))")
_ASCII_IDENTIFIER = re.compile(r"[A-Za-z0-9_]")
_SUP_CITATION_TAG = re.compile(r"<sup>([^<>]{1,60})</sup>")
_SUP_SOURCE_CONTENT = re.compile(r"^[0-9０-９\s,;\-–—]+$")
_SUP_TARGET_CONTENT = re.compile(r"^[0-9０-９\s,;，、；\-–—]+$")
_SUP_LEADING_SIGN = re.compile(r"^\s*[+\-−–—－]")
_SUP_PUNCT_BEFORE = set(".,;:?!)'\"”’。，、；：？！）》」』…—–")
_SUP_OP_BEFORE = set("=+-*/([<>\x00")
_SUP_OP_AFTER = set("=+-*/<>×·≤≥≈")

type _Citation = int | tuple[int, int]


def _citation_numbers(content: str) -> list[tuple[int, int | None]]:
    parsed = []
    for part in _CITATION_SEPARATORS.split(content.translate(_FULLWIDTH_DIGITS)):
        item = _CITATION_ITEM.fullmatch(part)
        if not item:
            return []
        start, end = int(item[1]), int(item[2]) if item[2] else None
        if end is not None and end <= start:
            return []
        parsed.append((start, end))
    return parsed


def _is_call_result_before(text: str, pos: int) -> bool:
    """仅当紧邻的半角闭括号对应开括号前紧贴 ASCII 标识符且括号内非纯自然语言短语时，判定为函数调用下标。"""
    if text[pos] != ")":
        return False
    depth = 0
    for idx in range(pos, -1, -1):
        ch = text[idx]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                if idx == 0 or _ASCII_IDENTIFIER.fullmatch(text[idx - 1]) is None:
                    return False
                inside = text[idx + 1 : pos].strip()
                # 如 Linux(Seccomp Notify)[18]：去空格后括号内是空格分隔的自然语言词组而非代码参数。
                return not (re.search(r"[A-Za-z]\s+[A-Za-z]", inside) and not re.search(r"[,=+\-*/<>.:()\[\]]", inside))
    return False


def _citation_context(
    text: str,
    start: int,
    end: int,
    *,
    target: bool,
    interval_like: bool = False,
    single_positive: bool = False,
) -> tuple[bool, bool, int]:
    left, right = start, end
    while left and text[left - 1].isspace():
        left -= 1
    while right < len(text) and text[right].isspace():
        right += 1
    first, last = text[right : right + 1], text[left - 1 : left] if left else ""
    word_start = left
    while word_start and _ASCII_IDENTIFIER.fullmatch(text[word_start - 1]):
        word_start -= 1
    word = text[word_start:left]
    name = word.lower()
    call_index = left == start and last in (")", "）") and _is_call_result_before(text, left - 1)
    index = (
        (
            bool(word and not word[0].isdigit())
            and (len(word) == 1 or "_" in word or (left == start and any(c.isdigit() for c in word)))
        )
        or (left == start and name in _INDEX_NAMES)
        or call_index
    )
    math_in = False
    if name == "in" and interval_like:
        prev_end = word_start
        while prev_end and text[prev_end - 1].isspace():
            prev_end -= 1
        if prev_end and text[prev_end - 1] == "\x00":
            math_in = True
        else:
            prev_start = prev_end
            while prev_start and _ASCII_IDENTIFIER.fullmatch(text[prev_start - 1]):
                prev_start -= 1
            prev_word = text[prev_start:prev_end]
            math_in = bool(prev_word and not prev_word[0].isdigit()) and (
                len(prev_word) == 1 or "_" in prev_word or prev_word.lower() in _MATH_IN_SUBJECTS
            )
    data_name = not single_positive and name in _DATA_NAMES
    zh_data_name = target and not single_positive and text.endswith(_DATA_NAMES_ZH, 0, left)
    zh_interval = (
        target
        and interval_like
        and text.endswith(_ZH_INTERVAL_PREFIX, 0, left)
        and (text.endswith("属于", 0, left) or _ZH_INTERVAL_SUFFIX.match(text, right) is not None)
    )
    # 左括号也会排除替换 URL 后留下的 Markdown 链接目标。
    hard_excluded = (
        index
        or data_name
        or math_in
        or first in ("=", "[", "［", "(")
        or last in ("=", "+", "-", "*", "/", "∈", "⊂", "⊆")
    )
    soft_zh_excluded = zh_data_name or zh_interval
    explicit = _CITATION_PREFIX.search(text, max(0, left - 12), left) is not None
    return hard_excluded, soft_zh_excluded, 9999 if explicit else 999


def _expand_citation_items(numbers: Sequence[tuple[int, int | None]]) -> list[_Citation]:
    anchors: list[_Citation] = []
    for a, b in numbers:
        if b is None:
            anchors.append(a)
        elif b - a <= 30:
            anchors.extend(range(a, b + 1))
        else:
            anchors.append((a, b))
    return anchors


def _citation_anchors(
    text: str, *, target: bool = False, expected: Mapping[_Citation, int] | None = None
) -> tuple[Counter[_Citation], Counter[_Citation], set[tuple[int, ...]]]:
    view = _CITATION_BOUNDARY.sub("\x00", RICH_TAG.sub("", text))
    pattern = _TARGET_BRACKETS if target else _SOURCE_BRACKETS
    locator_pattern = _TARGET_LOCATOR_BRACKET if target else _SOURCE_LOCATOR_BRACKET
    groups = [(m.start(), m.end(), _citation_numbers(m[1] or m[2])) for m in pattern.finditer(view)]
    groups.extend(
        (m.start(), m.end(), _citation_numbers(m[1] or (m[2] if m.lastindex and m.lastindex >= 2 else "")))
        for m in locator_pattern.finditer(view)
    )
    groups.sort(key=lambda item: item[0])
    confirmed: Counter[_Citation] = Counter()
    excluded: Counter[_Citation] = Counter()
    orders: set[tuple[int, ...]] = set()
    i = 0
    while i < len(groups):
        start, end, numbers = groups[i]
        i += 1
        if not numbers:
            continue
        # 相邻数字括号可表示多条引文，先合看两侧以区分多维索引和 [1][2]。
        chain = [numbers]
        while i < len(groups) and groups[i][0] == end and groups[i][2]:
            _, end, numbers = groups[i]
            chain.append(numbers)
            i += 1
        interval_like = (
            len(chain) == 1
            and len(chain[0]) == 2
            and chain[0][0][1] is None
            and chain[0][1][1] is None
            and chain[0][0][0] < chain[0][1][0]
        )
        single_positive = len(chain) == 1 and len(chain[0]) == 1 and chain[0][0][1] is None and chain[0][0][0] >= 1
        hard_blocked, soft_zh_blocked, upper = _citation_context(
            view, start, end, target=target, interval_like=interval_like, single_positive=single_positive
        )
        for numbers in chain:
            anchors = _expand_citation_items(numbers)
            blocked = hard_blocked or (
                soft_zh_blocked and not (expected is not None and anchors and all(a in expected for a in anchors))
            )
            valid = not blocked and all(1 <= a <= (b if b is not None else a) <= upper for a, b in numbers)
            if valid:
                confirmed.update(anchors)
                if 2 <= len(anchors) <= 15 and len(set(anchors)) == len(anchors):
                    seq = tuple(a for a in anchors if isinstance(a, int))
                    if len(seq) == len(anchors):
                        orders.add(seq)
            else:
                excluded.update(anchors)
    return confirmed, excluded, orders


def _sup_citation_anchors(
    text: str, *, target: bool = False, expected: Mapping[_Citation, int] | None = None
) -> Counter[_Citation]:
    """提取紧贴标点、词尾或公式占位符的无括号上标数字引文（排除数字/单位/单字母幂次与电荷）。"""
    view = _PLACEHOLDER.sub("\x01", _URL_RE.sub("\x00", _SCRIPT_STYLES.sub("", text)))
    content_re = _SUP_TARGET_CONTENT if target else _SUP_SOURCE_CONTENT
    anchors: Counter[_Citation] = Counter()
    for match in _SUP_CITATION_TAG.finditer(view):
        raw = match[1].strip()
        if not raw or _SUP_LEADING_SIGN.match(raw) or not content_re.fullmatch(raw):
            continue
        numbers = _citation_numbers(raw)
        if not numbers or not all(1 <= a <= (b if b is not None else a) <= 999 for a, b in numbers):
            continue
        left = match.start()
        if left > 0 and view[left - 1] in (" ", "\t", "\u00a0"):
            left -= 1
        if left == 0:
            continue
        prev_ch = view[left - 1]
        if prev_ch in _SUP_OP_BEFORE or prev_ch.isdigit():
            continue
        single_val = len(numbers) == 1 and numbers[0][1] is None
        if prev_ch in _SUP_PUNCT_BEFORE or _HAN.fullmatch(prev_ch):
            accepted = True
        elif prev_ch == "\x01":
            if not single_val:
                accepted = True
            elif target:
                accepted = expected is not None and numbers[0][0] in expected
            else:
                right = match.end()
                while right < len(view) and view[right] in (" ", "\t", "\u00a0"):
                    right += 1
                next_ch = view[right] if right < len(view) else ""
                accepted = numbers[0][0] >= 10 and next_ch not in _SUP_OP_AFTER
        elif _ASCII_IDENTIFIER.fullmatch(prev_ch):
            word_start = left
            while word_start and _ASCII_IDENTIFIER.fullmatch(view[word_start - 1]):
                word_start -= 1
            word = view[word_start:left]
            if not single_val:
                accepted = bool(word and not word[0].isdigit())
            else:
                accepted = (
                    len(word) >= 2 and not word[0].isdigit() and _SUP_BASE.search(view, max(0, left - 5), left) is None
                )
        else:
            accepted = False
        if accepted:
            anchors.update(_expand_citation_items(numbers))
    return anchors


def _citation_sort_key(anchor: _Citation) -> tuple[int, int]:
    return anchor if isinstance(anchor, tuple) else (anchor, anchor)


def _format_citation(anchor: _Citation) -> str:
    return f"{anchor[0]}–{anchor[1]}" if isinstance(anchor, tuple) else str(anchor)


def citation_problems(source: str, target: str) -> list[str]:
    """比对原文可确认引文（方括号、混合定位及上标数字引文），并处理与数学/代码共用的歧义编号。"""
    problems: list[str] = []
    want, excluded, source_orders = _citation_anchors(source)
    if want:
        got, _, target_orders = _citation_anchors(target, target=True, expected=want)
        for anchor in excluded.keys() - want.keys():
            got.pop(anchor, None)
        for anchor in sorted(want.keys() | got.keys(), key=_citation_sort_key):
            low = want[anchor]
            high = low + excluded[anchor] if low else 0
            actual = got[anchor]
            if actual < low:
                problems.append(f"缺少引文 [{_format_citation(anchor)}]（{low - actual} 次）")
            elif actual > high:
                problems.append(f"多出引文 [{_format_citation(anchor)}]（{actual - high} 次）")
        source_groups = {tuple(sorted(seq)) for seq in source_orders}
        for seq in sorted(target_orders - source_orders):
            if tuple(sorted(seq)) in source_groups:
                problems.append(f"引文编号顺序与原文不一致：[{', '.join(map(str, seq))}] 应保持原文顺序")
        if source_orders:
            view = _CITATION_BOUNDARY.sub("\x00", RICH_TAG.sub("", target)).translate(_FULLWIDTH_DIGITS)
            for match in _TARGET_BRACKETS.finditer(view):
                raw = match[1] or match[2]
                if not re.fullmatch(r"\s*\d{1,4}(?:\s+\d{1,4})+\s*", raw):
                    continue
                seq = tuple(map(int, raw.split()))
                if tuple(sorted(seq)) in source_groups:
                    hard, soft, _ = _citation_context(view, *match.span(), target=True, interval_like=len(seq) == 2)
                    if not (hard or soft):
                        problems.append(f"引文 {match[0]} 缺少编号之间的分隔符，请保持原文分隔")

    sup_want = _sup_citation_anchors(source)
    if sup_want:
        sup_got = _sup_citation_anchors(target, target=True, expected=sup_want)
        quant_sups = {int(value) for tag, _base, value in _script_values(source) if tag == "sup" and value.isdigit()}
        for num in quant_sups - sup_want.keys():
            sup_got.pop(num, None)
        for counts, detail in ((sup_want - sup_got, "缺少"), (sup_got - sup_want, "多出")):
            for anchor, count in counts.items():
                problems.append(f"{detail}上标引文 <sup>{_format_citation(anchor)}</sup>（{count} 次）")
    return problems


_SCRIPT_STYLES = re.compile(r"</?[bi]>")
_SCRIPT = re.compile(r"<(sup|sub)>([^<>]*)</\1>")
_SCRIPT_TRANSLATION = _FULLWIDTH_DIGITS | str.maketrans("−–—－", "----")
_SCRIPT_VALUE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
_SUB_BASE = re.compile(r"(?<![A-Za-z])[A-Za-z]{1,2}$")
_SUP_BASE = re.compile(r"(?<![A-Za-z])(?:m|cm|mm|km|s|ms|ns|Hz|kHz|MHz|GHz|kg|g|mg|K|Pa|R)$|[0-9]$")
_YEAR_END = re.compile(r"(?:19|20)[0-9]{2}$")

type _ScriptValue = tuple[str, str | None, str]


def _script_values(text: str) -> Counter[_ScriptValue]:
    view = _SCRIPT_STYLES.sub("", text).translate(_SCRIPT_TRANSLATION)
    values: Counter[_ScriptValue] = Counter()
    for match in _SCRIPT.finditer(view):
        tag, payload = match[1], "".join(match[2].split())
        if not _SCRIPT_VALUE.fullmatch(payload):
            continue
        end = match.start()
        start = max(0, end - 5)
        value = payload.removeprefix("+")
        if tag == "sub":
            base = _SUB_BASE.search(view, start, end)
            values[tag, base[0] if base else None, value] += 1
        elif payload.startswith(("+", "-")) or (
            any(c in "123456789" for c in payload)
            and _SUP_BASE.search(view, start, end)
            and not _YEAR_END.search(view, start, end)
        ):
            values[tag, None, value] += 1
    return values


def script_value_problems(source: str, target: str) -> list[str]:
    """结构校验通过后，比对具有明确定量语义的上下标。"""
    want, got = _script_values(source), _script_values(target)
    problems = []
    for counts, detail in ((want - got, "缺少"), (got - want, "多出")):
        for (tag, base, value), count in counts.items():
            anchor = f"{base or ''}<{tag}>{value}</{tag}>"
            problems.append(f"{detail}{_TAG_LABELS[tag]}数值 {anchor}（{count} 次）")
    return problems


_PCT_RANGE = re.compile(
    r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:[-–—~～]|to|至|到)\s*(\d+(?:[.,]\d+)?)\s*[%％]",
    re.I,
)
_PCT_SINGLE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*[%％]")
_PCT_ZH_RANGE = re.compile(r"百分之\s*(\d+(?:[.,]\d+)?)\s*(?:[-–—~～]|至|到)\s*(\d+(?:[.,]\d+)?)")
_PCT_ZH_SINGLE = re.compile(r"百分之\s*(\d+(?:[.,]\d+)?)")
_PCT_ZH_HUNDRED = re.compile(r"百分之百|百分百")
_DECIMAL_TOKEN = re.compile(r"(?<!\d)(\d+[.,]\d+|\.\d+)(?!\d)")
_FORMULA_DIGIT_SPACE = re.compile(r"(?<=\d)\s+(?=\d)")
_FORMULA_DEC_SPACE = re.compile(r"(?<=\d)\s*([.,])\s*(?=\d)")
_FORMULA_PCT_ESCAPE = re.compile(r"(?<=\d)\s*\\%")


def _parse_decimal(raw: str) -> Decimal | None:
    cleaned = raw.strip()
    if not cleaned:
        return None
    if "," in cleaned and "." not in cleaned:
        parts = cleaned.split(",")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            cleaned = (
                cleaned.replace(",", "") if len(parts[1]) == 3 and int(parts[0]) >= 1 else cleaned.replace(",", ".")
            )
        else:
            return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _collapse_formula_numbers(latex: str) -> str:
    """折叠公式 LaTeX 中数字与小数点/百分号间的空格（如 '0 . 0 5' -> '0.05'）。"""
    collapsed = _FORMULA_PCT_ESCAPE.sub("%", latex)
    collapsed = _FORMULA_DEC_SPACE.sub(r"\1", collapsed)
    return _FORMULA_DIGIT_SPACE.sub("", collapsed)


def _extract_percentages(text: str) -> list[tuple[str, Decimal]]:
    view = _URL_RE.sub(" ", RICH_TAG.sub("", _PLACEHOLDER.sub(" ", text))).translate(_FULLWIDTH_DIGITS)
    found: list[tuple[str, Decimal]] = []
    consumed: list[tuple[int, int]] = []

    def add(raw: str) -> None:
        val = _parse_decimal(raw)
        if val is not None:
            found.append((raw, val))

    for pattern in (_PCT_RANGE, _PCT_ZH_RANGE):
        for match in pattern.finditer(view):
            add(match.group(1))
            add(match.group(2))
            consumed.append((match.start(), match.end()))

    def overlaps_consumed(start: int, end: int) -> bool:
        return any(a < end and start < b for a, b in consumed)

    for pattern in (_PCT_SINGLE, _PCT_ZH_SINGLE):
        for match in pattern.finditer(view):
            if overlaps_consumed(match.start(), match.end()):
                continue
            add(match.group(1))

    for match in _PCT_ZH_HUNDRED.finditer(view):
        if not overlaps_consumed(match.start(), match.end()):
            found.append(("100", Decimal("100")))
    return found


def _extract_decimals(text: str) -> set[Decimal]:
    view = _URL_RE.sub(" ", RICH_TAG.sub("", _PLACEHOLDER.sub(" ", text))).translate(_FULLWIDTH_DIGITS)
    decimals: set[Decimal] = set()
    for match in _DECIMAL_TOKEN.finditer(view):
        val = _parse_decimal(match.group(1))
        if val is not None:
            decimals.add(val)
    return decimals


def percentage_problems(source: str, target: str, *, formulas: Mapping[str, str] | None = None) -> list[str]:
    """校验百分数事实锚点：拦截译文凭空新增或遗漏的百分数，允许与原文精确小数或 1-α 补数等价的写法。"""
    src_items = _extract_percentages(source)
    tgt_items = _extract_percentages(target)
    if not src_items and not tgt_items:
        return []

    formula_text = " ".join(_collapse_formula_numbers(v) for v in (formulas or {}).values())
    src_allowed_pcts = {val for _, val in src_items}
    if formula_text:
        src_allowed_pcts.update(val for _, val in _extract_percentages(formula_text))
    src_decimals = _extract_decimals(source)
    if formula_text:
        src_decimals.update(_extract_decimals(formula_text))

    tgt_pcts = {val for _, val in tgt_items}
    tgt_decimals = _extract_decimals(target)
    hundred = Decimal(100)
    problems: list[str] = []

    reported_extra: set[Decimal] = set()
    for raw, val in tgt_items:
        if val in src_allowed_pcts or val in reported_extra:
            continue
        ratio = val / hundred
        complement = (hundred - val) / hundred
        if ratio in src_decimals or (0 < val < hundred and complement in src_decimals):
            continue
        reported_extra.add(val)
        problems.append(f"译文新增了原文不存在的百分数 {raw}%，请严格忠实原文数值")

    reported_missing: set[Decimal] = set()
    for raw, val in src_items:
        if val in tgt_pcts or val in reported_missing:
            continue
        if (val / hundred) in tgt_decimals:
            continue
        reported_missing.add(val)
        problems.append(f"译文遗漏了原文百分数 {raw}%")

    return problems


_ENGLISH_WORD = re.compile(r"[A-Za-z]{2,}(?:'[A-Za-z]+)?")
_PROSE_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from", "has", "have", "in", "is", "of",
    "on", "or", "so", "that", "the", "their", "this", "to", "was", "were", "when", "which", "with",
}  # fmt: skip
_NON_PROSE = re.compile(
    r"\b(?:https?://|www\.)[^\s<>()]+|"
    r"(?<![\w./\\])(?:[A-Za-z]:[\\/]|(?:\.{1,2}|~)[\\/])[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)*|"
    r"(?<![\w./\\])[A-Za-z0-9_.-]*(?:[\\/][A-Za-z0-9_.-]+){2,}|"
    r"(?<![\w./\\])[A-Za-z0-9_.-]*[\\/][A-Za-z0-9_.-]+\.(?i:csv|html|json|md|pdf|png|py|tex|tsv|txt|xml|ya?ml)\b|"
    r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]|"
    r"\([A-Z][A-Za-z'’.-]+(?:\s+(?:et al\.|and [A-Z][A-Za-z'’.-]+)|\s*&\s*[A-Z][A-Za-z'’.-]+)?"
    r",?\s+(?:19|20)\d{2}[a-z]?\)"
)
_LEAKED_COMMON_WORDS = frozenset({
    "algorithms", "approaches", "architectures", "datasets", "equations", "experiments",
    "layers", "lemmas", "methods", "models", "networks", "problems", "properties",
    "representations", "results", "strategies", "structures", "systems", "techniques", "theorems",
})  # fmt: skip
_PROTECTED_ENGLISH_SPAN = re.compile(
    r"`[^`\r\n]+`|（[^（）\r\n]{1,120}）|\([^()\r\n]{1,120}\)|\[[^\[\]\r\n]{1,120}\]|"
    r"“[^“”\r\n]{1,120}”|\"[^\"\r\n]{1,120}\"|‘[^‘’\r\n]{1,120}’|「[^「」\r\n]{1,120}」|『[^『』\r\n]{1,120}』"
)
_LEAKED_WORD_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_/\\-])(?<![A-Za-z0-9_]\.)([a-z]{5,})(?![A-Za-z0-9_/\\-])(?!\.[A-Za-z0-9_])"
)
_CHINESE_BOUNDARY_PUNCT = set("，。、；：？！—–….,;:?!")


def _plain_prose(text: str) -> str:
    return _NON_PROSE.sub(" ", _RICH_TAG.sub("", _PLACEHOLDER.sub(" ", text)))


def _leaked_watch_words(translated_terms: Mapping[str, str] | None) -> frozenset[str]:
    del translated_terms
    return _LEAKED_COMMON_WORDS


def _leaked_english_words(
    original: str, translated: str, *, translated_terms: Mapping[str, str] | None = None
) -> list[str]:
    """检测孤立夹杂在中文句子中的纯小写普通英文漏译词（对专名/代码/缩写/括号注释保持 0 误报）。"""
    if len(_HAN.findall(translated)) < 4:
        return []
    source_lower_words = set(_LEAKED_WORD_CANDIDATE.findall(original))
    watch = _leaked_watch_words(translated_terms) & source_lower_words
    if not watch:
        return []
    cleaned = _PROTECTED_ENGLISH_SPAN.sub(" ", translated)
    leaked: list[str] = []
    for match in _LEAKED_WORD_CANDIDATE.finditer(cleaned):
        word = match.group(1)
        if word not in watch:
            continue
        left = match.start()
        if left > 0 and cleaned[left - 1] in (" ", "\t", "\u00a0"):
            left -= 1
        right = match.end()
        if right < len(cleaned) and cleaned[right] in (" ", "\t", "\u00a0"):
            right += 1
        before = cleaned[left - 1] if left > 0 else ""
        after = cleaned[right] if right < len(cleaned) else ""
        before_han = bool(before and _HAN.fullmatch(before))
        after_han = bool(after and _HAN.fullmatch(after))
        before_ok = before_han or not before or before in _CHINESE_BOUNDARY_PUNCT
        after_ok = after_han or not after or after in _CHINESE_BOUNDARY_PUNCT
        if (before_han or after_han) and before_ok and after_ok and word not in leaked:
            leaked.append(word)
    return leaked


def prose_problems(source: str, target: str, *, translated_terms: Mapping[str, str] | None = None) -> list[str]:
    """只对足够明确的英文正文应用低误报筛查。"""
    original = _plain_prose(source)
    tokens = _ENGLISH_WORD.findall(original)
    words = [word.lower() for word in tokens]
    letters = sum(map(len, words))
    source_han = len(_HAN.findall(original))
    translated = _plain_prose(target)
    if source_han * 5 < max(len(words), 1) and (
        leaked := _leaked_english_words(original, translated, translated_terms=translated_terms)
    ):
        return [f"译文夹杂未翻译的英文普通词：{', '.join(leaked)}，请译为中文"]
    if len(words) < 8 or letters < 35 or source_han * 5 >= len(words) or len(_PROSE_WORDS.intersection(words)) < 2:
        return []
    # 短标题常用实词首字母大写，配置名与这类标题都可能沿用原文。
    content_words = [word for word in tokens if word.lower() not in _PROSE_WORDS]
    if (
        len(words) <= 14
        and content_words
        and sum(word[0].isupper() for word in content_words) * 4 >= len(content_words) * 3
    ):
        return []
    han = len(_HAN.findall(translated))
    english = _ENGLISH_WORD.findall(translated.lower())
    if han <= max(2, len(words) // 10):
        similarity = SequenceMatcher(None, words, english)
        if similarity.quick_ratio() >= 0.85 and similarity.ratio() >= 0.85:
            return ["疑似未翻译（照抄原文）"]
    # 忽略简短的中文引导语时，要求后面保留了完整的英文词序列。
    if 0 < han <= 4 and english == words and not _HAN.search(re.split(r"[A-Za-z]", translated, maxsplit=1)[-1]):
        return ["疑似未翻译（照抄原文）"]
    if len(words) >= 20 and letters >= 90:
        visible = sum(c.isalnum() for c in translated)
        if visible < max(6, letters * 0.055):
            return ["译文严重缩水，疑似漏译"]
        if visible > letters * 2.2:
            return ["译文长度异常暴涨，疑似复读"]
    if han >= 24 and re.fullmatch(r"(.{12,}?)\1{3,}", "".join(translated.split())):
        return ["译文疑似复读"]
    return []
