"""按可见词窗查表，保留原文区间供三层术语仲裁。"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pdftr.domain.text import RICH_TAG, normalize_term_key
from pdftr.glossary.filters import is_lazy_english_self_map

if TYPE_CHECKING:
    from pdftr.glossary.extract import Extraction

MAX_WORDS = 6
TERM_TOKEN = re.compile(r"\w+(?:(?:[-'/@.+#]|:{1,2})\w+)*(?:\+{1,2}|#)?")
_BLOCKER = re.compile(r"<m[0-9]+/>|(?:https?://|www\.)\S+", re.I)
HORIZONTAL_SPACE = r"[^\S\r\n\v\f\x1c-\x1e\x85\u2028\u2029]"
_TERM_GAP = re.compile(rf"(?:{HORIZONTAL_SPACE}|-)+")


def mask_term_boundaries(text: str) -> str:
    return _BLOCKER.sub(lambda m: "\0" * len(m[0]), text)


def _visible_text(text: str) -> tuple[str, Sequence[int]]:
    end = 0
    parts: list[str] = []
    offsets: list[int] = []
    for tag in RICH_TAG.finditer(text):
        parts.append(text[end : tag.start()])
        offsets.extend(range(end, tag.start()))
        end = tag.end()
    if not parts:
        return mask_term_boundaries(text), range(len(text))
    offsets.extend(range(end, len(text)))
    parts.append(text[end:])
    return mask_term_boundaries("".join(parts)), offsets


def term_windows(text: str, max_words: int) -> Iterator[tuple[int, int, str]]:
    """按起点升序、同起点按长度降序枚举词窗。"""
    tokens = list(TERM_TOKEN.finditer(text))
    for i, token in enumerate(tokens):
        start = token.start()
        last = i
        while last + 1 < min(len(tokens), i + max_words):
            if not _TERM_GAP.fullmatch(text[tokens[last].end() : tokens[last + 1].start()]):
                break
            last += 1
        for j in range(last, i - 1, -1):
            end = tokens[j].end()
            yield start, end, text[start:end]


@dataclass(frozen=True, slots=True)
class TermMatch:
    start: int
    end: int
    key: str
    term: str
    target: str


class TermMatcher:
    def __init__(self, terms: Mapping[str, str], min_length: int = 4) -> None:
        self._table: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._max_words = 1
        for term, target in terms.items():
            spelling = term.strip()
            key = normalize_term_key(spelling)
            words = len(TERM_TOKEN.findall(spelling))
            # 太短的词条（如 “AI”“ML”）容易误命中，交给模型自己处理。
            minimum = min(min_length, 3) if key.startswith("acr:") else min_length
            if key and 0 < words <= MAX_WORDS and len(spelling) >= minimum:
                self._table[key].append((term, target))
                # 连字符词也能以分开的词形出现。
                self._max_words = max(self._max_words, words, min(MAX_WORDS, len(key.split())))

    def __len__(self) -> int:
        return len(self._table)

    def _match_spans(
        self,
        visible: str,
        offsets: Sequence[int],
        occupied: Sequence[TermMatch] = (),
        accept: Callable[[str, str, str], bool] | None = None,
    ) -> list[TermMatch]:
        if not self._table:
            return []
        taken_ends = [m.end for m in occupied]
        hits: list[TermMatch] = []
        for start, end, written in term_windows(visible, self._max_words):
            start, end = offsets[start], offsets[end - 1] + 1
            if hits and start < hits[-1].end:
                continue
            i = bisect_right(taken_ends, start)
            if i < len(occupied) and occupied[i].start < end:
                continue
            key = normalize_term_key(written)
            if bucket := self._table.get(key):
                exact = next((entry for entry in bucket if entry[0].strip() == written), None)
                if exact is not None and (accept is None or accept(key, *exact)):
                    hits.append(TermMatch(start, end, key, *exact))
                    continue
                for term, target in bucket:
                    if (term, target) != exact and (accept is None or accept(key, term, target)):
                        hits.append(TermMatch(start, end, key, term, target))
                        break
        return hits

    def match_spans(self, text: str) -> list[TermMatch]:
        return self._match_spans(*_visible_text(text))

    def match(self, text: str, accept: Callable[[str, str, str], bool] | None = None) -> dict[str, str]:
        return {m.term: m.target for m in self._match_spans(*_visible_text(text), accept=accept)}


def _key_tokens(term: str) -> tuple[str, ...]:
    """将术语拆分为带类别的规范词元序列，用于检测复合词是否包含更短的用户术语。"""
    tokens: list[str] = []
    for match in TERM_TOKEN.finditer(term):
        key = normalize_term_key(match[0])
        if not key:
            continue
        if key.startswith("word:"):
            tokens.extend(f"word:{part}" for part in key.removeprefix("word:").split())
        else:
            tokens.append(key)
    return tuple(tokens)


def _has_strict_subsequence(seq: tuple[str, ...], sub: tuple[str, ...]) -> bool:
    if not sub or len(sub) >= len(seq):
        return False
    width = len(sub)
    return any(seq[i : i + width] == sub for i in range(len(seq) - width + 1))


def _compact_target(text: str) -> str:
    return "".join(text.split())


class TermSelection:
    """翻译与术语面板共用的三级术语选择。"""

    def __init__(
        self, found: Extraction, user_terms: Mapping[str, str], load_library: Callable[[], TermMatcher | None]
    ) -> None:
        user_by_key: dict[str, str] = {}
        user_sub_rules: list[tuple[tuple[str, ...], str]] = []
        for term, target in user_terms.items():
            if key := normalize_term_key(term):
                user_by_key.setdefault(key, target)
            if (tokens := _key_tokens(term)) and (compact := _compact_target(target)):
                user_sub_rules.append((tokens, compact))

        paper: dict[str, str] = {}
        for term, target in found.terms.items():
            key = normalize_term_key(term)
            if not key:
                continue
            if key in user_by_key:
                paper[term] = user_terms.get(term, user_by_key[key])
                continue
            if is_lazy_english_self_map(term, target):
                continue
            paper_tokens = _key_tokens(term)
            compact_target = _compact_target(target)
            if any(
                _has_strict_subsequence(paper_tokens, user_tokens) and user_target not in compact_target
                for user_tokens, user_target in user_sub_rules
            ):
                continue
            paper[term] = target

        self.paper = paper
        self._user = TermMatcher(user_terms, min_length=2)
        self._paper = TermMatcher(self.paper, min_length=2)
        self._library = load_library() if found.use_library else None

    def match_segment(self, text: str) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        visible, offsets = _visible_text(text)
        user = self._user._match_spans(visible, offsets)
        paper = self._paper._match_spans(visible, offsets, user)
        library: list[TermMatch] = []
        if self._library:
            occupied = sorted(user + paper, key=lambda m: m.start)
            higher_targets: dict[str, set[str]] = defaultdict(set)
            for hit in occupied:
                higher_targets[hit.key].add(hit.target)

            def allowed(key: str, _term: str, target: str) -> bool:
                targets = higher_targets.get(key)
                return targets is None or targets == {target}

            library = self._library._match_spans(visible, offsets, occupied, allowed)
        return (
            {m.term: m.target for m in user},
            {m.term: m.target for m in paper},
            {m.term: m.target for m in library},
        )

    def match_user(self, text: str) -> dict[str, str]:
        return self.match_segment(text)[0]

    def match_paper(self, text: str) -> dict[str, str]:
        return self.match_segment(text)[1]

    def match_library(self, text: str) -> dict[str, str]:
        return self.match_segment(text)[2]

    @property
    def library_size(self) -> int:
        return len(self._library) if self._library else 0
