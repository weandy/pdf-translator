"""分批与结果校验。

S7：约 1500 token 一批，比逐段请求或小批次省约 64% 输入；“编号|原文”紧凑格式，只要求输出译文。
"""

import re
from collections import Counter
from collections.abc import Sequence
from difflib import SequenceMatcher

BATCH_TOKENS = 1500
_PLACEHOLDER = re.compile(r"<m\d+/>")
_RICH_TAG = re.compile(r"<(/?)(b|i|sup|sub)>")


def estimate_tokens(text: str) -> int:
    """粗估 token：英文约 4 字符 1 token，中日韩字符约 1 字 1 token。够用于分批和费用预估。"""
    cjk = len(re.findall(r"[\u3000-\u9fff\uff00-\uffef]", text))
    return cjk + (len(text) - cjk) // 4 + 1


def make_batches[T](items: Sequence[T], sizes: Sequence[int], limit: int = BATCH_TOKENS) -> list[list[T]]:
    batches: list[list[T]] = []
    cur: list[T] = []
    total = 0
    for item, size in zip(items, sizes, strict=True):
        if cur and total + size > limit:
            batches.append(cur)
            cur, total = [], 0
        cur.append(item)
        total += size
    if cur:
        batches.append(cur)
    return batches


class NumberedResponseError(ValueError):
    """模型返回的编号无法可靠地匹配输入。"""


def parse_numbered(text: str, *, batch_size: int | None = None) -> dict[int, str]:
    """解析「编号|译文」。没有编号的行视为上一行的续行（模型偶尔会在段内换行）。"""
    out: dict[int, str] = {}
    last: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        m = re.match(r"^(\d+)\s*[|｜]\s*(.*)$", line)
        if m:
            digits = m.group(1)
            # 防止极长的数字触发 Python 的整数转换限制。
            if len(digits) > 12:
                raise NumberedResponseError("编号越界")
            number = int(digits)
            if number < 1 or (batch_size is not None and number > batch_size):
                raise NumberedResponseError(f"编号 {number} 越界")
            if number in out:
                raise NumberedResponseError(f"编号 {number} 重复")
            if last is not None and number < last:
                raise NumberedResponseError(f"编号 {number} 乱序")
            last = number
            out[last] = m.group(2).strip()
        elif last is not None:
            previous = out[last]
            space = " " if re.search(r"[A-Za-z0-9]$", previous) and re.match(r"[A-Za-z0-9]", line) else ""
            out[last] = f"{previous}{space}{line}"
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
    """校验一种富文本强调标签的数量、配对、嵌套和非空内容。"""
    expected = source.count(f"<{tag}>")
    if not expected:
        opened, closed = target.count(f"<{tag}>"), target.count(f"</{tag}>")
        if opened or closed:
            if opened != closed:
                return [f"译文新增{label}标记且未完整配对"]
            return [f"译文新增{label}标记：原文没有{label}，实际 {opened} 对"]
        return []
    opened, closed = target.count(f"<{tag}>"), target.count(f"</{tag}>")
    if opened != closed:
        return [f"{label}标记未完整配对"]
    if opened < expected:
        return [f"{label}标记丢失：需要 {expected} 对，实际 {opened} 对"]
    if opened > expected:
        return [f"{label}标记多出：需要 {expected} 对，实际 {opened} 对"]
    return rich_text_problems(target, tag=tag)


_TAG_LABELS = {"b": "粗体", "i": "斜体", "sup": "上标", "sub": "下标"}


def rich_text_problems(target: str, *, tag: str | None = None) -> list[str]:
    """人工编辑检查整个栈，模型验收也可检查某一种标签的结构。"""
    stack: list[tuple[str, int]] = []
    for match in _RICH_TAG.finditer(target):
        closing, current = match.group(1), match.group(2)
        if not closing:
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
    """原文含局部粗体时，译文必须保留同样数量、成对且正确嵌套的非空强调。

    原文没有粗体时，译文也不能凭空新增粗体。整块粗体正文在进入翻译
    时不会生成 ``<b>`` 标签，模型若重新包住整段会让译文错误地变成粗体。
    这里只能保证标记结构，强调内容的语义对应仍由翻译模型负责。
    """
    return _emphasis_problems(source, target, tag="b", label="粗体")


def italic_problems(source: str, target: str) -> list[str]:
    """原文含局部斜体时，译文必须保留同样数量、成对且正确嵌套的非空强调。"""
    return _emphasis_problems(source, target, tag="i", label="斜体")


def script_problems(source: str, target: str) -> list[str]:
    return _emphasis_problems(source, target, tag="sup", label="上标") + _emphasis_problems(
        source, target, tag="sub", label="下标"
    )


_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
_ENGLISH_WORD = re.compile(r"[A-Za-z]{2,}(?:'[A-Za-z]+)?")
_PROSE_WORDS = frozenset(
    "a an and are as at be by can for from has have in is of on or so that the their this to was were when which with".split()
)
_NON_PROSE = re.compile(
    r"\b(?:https?://|www\.)[^\s<>()]+|"
    r"(?<!\w)(?:[A-Za-z]:[\\/]|\.{0,2}[\\/])?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+|"
    r"(?<!\w)[~/][A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*|"
    r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]|"
    r"\([A-Z][A-Za-z'’.-]+(?:\s+(?:et al\.|and [A-Z][A-Za-z'’.-]+)|\s*&\s*[A-Z][A-Za-z'’.-]+)?"
    r",?\s+(?:19|20)\d{2}[a-z]?\)"
)


def _plain_prose(text: str) -> str:
    return _NON_PROSE.sub(" ", _RICH_TAG.sub("", _PLACEHOLDER.sub(" ", text)))


def prose_problems(source: str, target: str) -> list[str]:
    """只对足够明确的英文正文应用低误报筛查。"""
    original = _plain_prose(source)
    words = _ENGLISH_WORD.findall(original.lower())
    letters = sum(map(len, words))
    if len(words) < 8 or letters < 35 or _HAN.search(original) or len(_PROSE_WORDS.intersection(words)) < 2:
        return []
    translated = _plain_prose(target)
    if len(_HAN.findall(translated)) <= 2:
        english = _ENGLISH_WORD.findall(translated.lower())
        similarity = SequenceMatcher(None, words, english)
        if similarity.quick_ratio() >= 0.85 and similarity.ratio() >= 0.85:
            return ["疑似未翻译（照抄原文）"]
    if len(words) >= 20 and letters >= 90:
        visible = sum(c.isalnum() for c in translated)
        if visible < min(8, letters * 0.08):
            return ["译文严重缩水，疑似漏译"]
        if visible > letters * 4:
            return ["译文长度异常暴涨，疑似复读"]
    return []
