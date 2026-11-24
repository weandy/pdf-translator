"""在原文中查找词库术语：按单词切分后做 n 元组查表，最长匹配优先。

不用逐条正则：GIST 中文词库有 6400+ 条，逐条匹配一篇论文要扫几千遍；查表只需扫一遍。
"""

import re
from collections import defaultdict
from collections.abc import Callable, Mapping

from pdftr.glossary.extract import Extraction

MAX_WORDS = 6
_WORD = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")


_GAP_OK = re.compile(r"[\s/-]+")


def _norm(word: str) -> str:
    """小写 + 去掉简单复数，让 networks / network、boxes / box 都能命中。"""
    w = word.lower()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and re.search(r"(?:s|x|z|ch|sh)es$", w):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _key(words: list[str]) -> str:
    parts = [part for w in words for part in w.split("-") if part]
    return " ".join(_norm(p) for p in parts)


def _acronym(term: str) -> bool:
    return term.isupper() and sum(c.isalpha() for c in term) >= 2 and len(term.split()) == 1


def _matches_acronym(window: list[str]) -> bool:
    raw = "".join(window)
    return raw.isupper() or (len(raw) >= 3 and raw.endswith("s") and raw[:-1].isupper())


class TermMatcher:
    def __init__(self, terms: Mapping[str, str], min_length: int = 4) -> None:
        # 太短的词条（如 “AI”“ML”）容易误命中，交给模型自己处理
        self._table: dict[str, tuple[str, str, bool]] = {}
        for term, target in terms.items():
            words = _WORD.findall(term)
            acronym = _acronym(term)
            minimum = min(min_length, 3) if acronym else min_length
            if words and len(words) <= MAX_WORDS and len(term) >= minimum:
                self._table.setdefault(_key(words), (term, target, acronym))

    def __len__(self) -> int:
        return len(self._table)

    def match(self, text: str, accept: Callable[[str, str, str], bool] | None = None) -> dict[str, str]:
        spans = list(_WORD.finditer(text))
        words = [m.group(0) for m in spans]
        hits: dict[str, str] = {}
        i = 0
        while i < len(words):
            for n in range(min(MAX_WORDS, len(words) - i), 0, -1):
                if n > 1 and any(
                    not _GAP_OK.fullmatch(text[spans[k].end() : spans[k + 1].start()]) for k in range(i, i + n - 1)
                ):
                    continue
                window = words[i : i + n]
                key = _key(window)
                found = self._table.get(key)
                if found is None:
                    continue
                term, target, strict = found
                if (strict and not _matches_acronym(window)) or (accept is not None and not accept(key, term, target)):
                    continue
                hits[term] = target
                i += n
                break
            else:
                i += 1
        return hits


class TermSelection:
    """翻译与术语面板共用的三级术语选择。"""

    def __init__(
        self, found: Extraction, user_terms: Mapping[str, str], load_library: Callable[[], TermMatcher | None]
    ) -> None:
        user_keys: dict[str, str] = {}
        user_norms: dict[str, str] = {}
        for term, target in user_terms.items():
            user_keys.setdefault(term.casefold(), target)
            if words := _WORD.findall(term):
                user_norms.setdefault(_key(words), target)
        self.paper: dict[str, str] = {}
        paper_keys: set[str] = set()
        for term, target in found.terms.items():
            folded = term.casefold()
            if folded not in paper_keys:
                words = _WORD.findall(term)
                override = user_keys.get(folded) or (user_norms.get(_key(words)) if words else None)
                self.paper[term] = override if override is not None else target
                paper_keys.add(folded)
        self._user = TermMatcher(user_terms, min_length=2)
        self._paper = TermMatcher(self.paper, min_length=2)
        self._library = load_library() if found.use_library else None
        normalized = {_key(_WORD.findall(term)) for term in (*self.paper, *user_terms)}
        self._higher_keys = paper_keys | user_keys.keys() | normalized
        self._phrase_targets: dict[str, set[str]] = defaultdict(set)
        if self._library:
            for term, target in (self.paper | dict(user_terms)).items():
                words = _WORD.findall(term)
                for start in range(len(words)):
                    for end in range(start + 1, min(start + MAX_WORDS, len(words)) + 1):
                        self._phrase_targets[_key(words[start:end])].add(target)

    def match_user(self, text: str) -> dict[str, str]:
        return self._user.match(text)

    def match_paper(self, text: str) -> dict[str, str]:
        return self._paper.match(text)

    def match_library(self, text: str) -> dict[str, str]:
        def allowed(key: str, term: str, target: str) -> bool:
            return self._higher_keys.isdisjoint((term.casefold(), key)) and all(
                target in higher for higher in self._phrase_targets.get(key, ())
            )

        return self._library.match(text, allowed) if self._library else {}

    @property
    def library_size(self) -> int:
        return len(self._library) if self._library else 0
