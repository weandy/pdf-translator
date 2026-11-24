"""术语表的程序校正（S10：只靠提示词管不住全大写标题和章节名混进术语表）。

- 术语按正文中最常见的写法改写大小写（ADAM → Adam）
- 正文中没出现的、等于某个章节标题的，丢弃
- 译法与原文相同（不译）的，统一成正文写法
"""

import re
from collections import Counter
from collections.abc import Iterable

_SECTION_ONLY = frozenset({
    "abstract", "acknowledgment", "acknowledgments", "appendix", "background", "conclusion", "conclusions",
    "discussion", "evaluation", "experimental setup", "experiments", "highlights", "introduction", "keywords",
    "method", "methodology", "methods", "overview", "references", "related work", "related works", "results",
    "results and analysis", "summary",
})  # fmt: skip


def _term_pattern(term: str) -> re.Pattern[str]:
    flex = re.sub(r"(?:\\ |-)+", r"[\\s-]+", re.escape(term.strip()))
    return re.compile(rf"(?<![\w-]){flex}(?![\w-])", flags=re.IGNORECASE)


def normalize_terms(terms: dict[str, str], body: str, headings: Iterable[str] = ()) -> dict[str, str]:
    heading_list = list(headings)
    heading_set = {
        re.sub(r"^(?:[IVXLCDM]+\.|[A-Z]\.|\d+(?:\.\d+)*\.?)\s*", "", h).strip().lower() for h in heading_list
    }
    heading_blob = "\n".join(heading_list)
    out: dict[str, str] = {}
    for term, target in terms.items():
        pat = _term_pattern(term)
        found = pat.findall(body)
        if not found:
            continue
        low = term.strip().lower()
        if low in heading_set:
            if low in _SECTION_ONLY:
                continue
            body_hits = len(found) - len(pat.findall(heading_blob))
            if body_hits < 2:
                continue
        written = term if term.isupper() and len(term) >= 2 and term in found else Counter(found).most_common(1)[0][0]
        out[written] = written if target.strip().lower() == term.strip().lower() else target.strip()
    return out
