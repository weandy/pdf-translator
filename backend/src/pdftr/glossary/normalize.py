"""按正文词形和出现次数校正模型抽取的术语。"""

import re
from collections import Counter
from collections.abc import Iterable, Sequence

from pdftr.domain.text import RICH_TAG, normalize_term_key
from pdftr.glossary.matcher import mask_term_boundaries, term_windows

_SECTION_ONLY = frozenset({
    "abstract", "acknowledgment", "acknowledgments", "appendix", "background", "conclusion", "conclusions",
    "discussion", "evaluation", "experimental setup", "experiments", "highlights", "introduction", "keywords",
    "method", "methodology", "methods", "overview", "references", "related work", "related works", "results",
    "results and analysis", "summary",
})  # fmt: skip
_HEADING_NUMBER = re.compile(r"^(?:[ivxlcdm]+[.)]|[a-z][.)]|\d+(?:\.\d+)*[.)]?)\s+")


def normalize_terms(terms: dict[str, str], body: str | Sequence[str], headings: Iterable[str] = ()) -> dict[str, str]:
    heading_lines = Counter(" ".join(RICH_TAG.sub("", h).casefold().split()) for h in headings)
    heading_names = {_HEADING_NUMBER.sub("", h) for h in heading_lines}
    if isinstance(body, str):
        # 旧调用可能把独立标题行一并传入。段落序列由调用方先排除标题。
        paragraphs: list[str] = []
        for line in body.splitlines():
            name = " ".join(RICH_TAG.sub("", line).casefold().split())
            if heading_lines[name] > 0:
                heading_lines[name] -= 1
            else:
                paragraphs.append(line)
        body = paragraphs

    entries: dict[str, Counter[str]] = {key: Counter() for term in terms if (key := normalize_term_key(term))}
    if not entries:
        return {}
    max_words = max(len(key.split()) for key in entries)
    for paragraph in body:
        visible = mask_term_boundaries(RICH_TAG.sub("", paragraph))
        for _, _, written in term_windows(visible, max_words):
            forms = entries.get(normalize_term_key(written))
            if forms is not None:
                forms[" ".join(written.split())] += 1

    out: dict[str, str] = {}
    for term, target in terms.items():
        forms = entries.get(normalize_term_key(term))
        low = " ".join(term.casefold().split())
        if not forms or low in _SECTION_ONLY or (low in heading_names and forms.total() < 2):
            continue
        written = forms.most_common(1)[0][0]
        target = target.strip()
        out[written] = written if target.casefold() == term.strip().casefold() else target
    return out
