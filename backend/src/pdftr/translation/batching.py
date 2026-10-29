"""分批与结果校验。

S7：约 1500 token 一批，比逐段请求或小批次省约 64% 输入；“编号|原文”紧凑格式，只要求输出译文。
"""

import re
from collections import Counter
from collections.abc import Sequence

BATCH_TOKENS = 1500
_PLACEHOLDER = re.compile(r"<m\d+/>")


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


def parse_numbered(text: str) -> dict[int, str]:
    """解析「编号|译文」。没有编号的行视为上一行的续行（模型偶尔会在段内换行）。"""
    out: dict[int, str] = {}
    last: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        m = re.match(r"^(\d+)\s*[|｜]\s*(.*)$", line)
        if m:
            last = int(m.group(1))
            out[last] = m.group(2).strip()
        elif last is not None:
            out[last] = f"{out[last]}{line}"
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
