"""在原文中查找词库术语：按单词切分后做 n 元组查表，最长匹配优先。

不用逐条正则：GIST 中文词库有 6400+ 条，逐条匹配一篇论文要扫几千遍；查表只需扫一遍。
"""

import re
from collections.abc import Callable, Mapping

MAX_WORDS = 6
_WORD = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")


def _norm(word: str) -> str:
    """小写 + 去掉简单复数，让 networks / network 都能命中。"""
    w = word.lower()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _key(words: list[str]) -> str:
    return " ".join(_norm(w) for w in words)


class TermMatcher:
    def __init__(self, terms: Mapping[str, str], min_length: int = 4) -> None:
        # 太短的词条（如 “AI”“ML”）容易误命中，交给模型自己处理
        self._table: dict[str, tuple[str, str]] = {}
        for term, target in terms.items():
            words = _WORD.findall(term)
            if words and len(words) <= MAX_WORDS and len(term) >= min_length:
                self._table.setdefault(_key(words), (term, target))

    def __len__(self) -> int:
        return len(self._table)

    def match(self, text: str) -> dict[str, str]:
        words = _WORD.findall(text)
        hits: dict[str, str] = {}
        i = 0
        while i < len(words):
            for n in range(min(MAX_WORDS, len(words) - i), 0, -1):
                found = self._table.get(_key(words[i : i + n]))
                if found:
                    hits[found[0]] = found[1]
                    i += n
                    break
            else:
                i += 1
        return hits


def combine(user_terms: Mapping[str, str], library: "TermMatcher | None") -> "Callable[[str], dict[str, str]] | None":
    """用户术语优先于公共词库：同一术语（不分大小写）以用户的译法为准。用户自己加的短缩写也收（最短 2 个字符）。"""
    user = TermMatcher(user_terms, min_length=2) if user_terms else None
    if user is None and library is None:
        return None

    def lookup(text: str) -> dict[str, str]:
        mine = user.match(text) if user else {}
        lowered = {k.lower() for k in mine}
        base = {k: v for k, v in library.match(text).items() if k.lower() not in lowered} if library else {}
        return base | mine

    return lookup


def override(terms: Mapping[str, str], user_terms: Mapping[str, str]) -> dict[str, str]:
    """自动抽取的文档术语里，用户术语库已有的（不分大小写）改用用户的译法。"""
    mine = {k.lower(): v for k, v in user_terms.items()}
    return {k: mine.get(k.lower(), v) for k, v in terms.items()}
