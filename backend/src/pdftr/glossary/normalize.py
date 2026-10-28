"""术语表的程序校正（S10：只靠提示词管不住全大写标题和章节名混进术语表）。

- 术语按正文中最常见的写法改写大小写（ADAM → Adam）
- 正文中没出现的、等于某个章节标题的，丢弃
- 译法与原文相同（不译）的，统一成正文写法
"""

import re
from collections import Counter
from collections.abc import Iterable


def normalize_terms(terms: dict[str, str], body: str, headings: Iterable[str] = ()) -> dict[str, str]:
    heading_set = {re.sub(r"^[\d.\s]+", "", h).strip().lower() for h in headings}
    out: dict[str, str] = {}
    for term, target in terms.items():
        found = re.findall(rf"(?<![\w-]){re.escape(term)}(?![\w-])", body, flags=re.IGNORECASE)
        if not found or term.lower() in heading_set:
            continue
        written = Counter(found).most_common(1)[0][0]
        out[written] = written if target.strip().lower() == term.strip().lower() else target.strip()
    return out
