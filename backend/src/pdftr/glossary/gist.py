"""GIST 多语言 AI 术语数据集（中文部分 6426 条，Apache-2.0）。

来源：Liu et al., "Towards Global AI Inclusivity: A Large-Scale Multilingual Terminology Dataset (GIST)",
https://huggingface.co/datasets/Jerry999/multilingual-terminology
数据不随仓库分发，首次使用时下载到 data/glossaries/vendor/。
"""

import csv
import urllib.request
from collections.abc import Iterable
from pathlib import Path

URL = "https://huggingface.co/datasets/Jerry999/multilingual-terminology/resolve/main/Chinese.csv"
ATTRIBUTION = "GIST (Liu et al., ACL Findings 2025), Apache-2.0"


def download(dest: Path, url: str = URL, timeout: float = 60) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=timeout) as r, tmp.open("wb") as f:  # 固定的 https 地址
        f.write(r.read())
    tmp.replace(dest)
    return dest


def load(path: Path) -> dict[str, str]:
    """读取 CSV（列：Index, English, Chinese）。同一英文词条出现多次时保留第一条。"""
    terms: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            en, zh = (row.get("English") or "").strip(), (row.get("Chinese") or "").strip()
            if en and zh:
                terms.setdefault(en, zh)
    return terms


class GistSource:
    name = "gist"

    def __init__(self, terms: dict[str, str]) -> None:
        self._lower = {k.lower(): v for k, v in terms.items()}
        self.terms = terms

    @classmethod
    def from_file(cls, path: Path) -> "GistSource":
        return cls(load(path))

    def lookup(self, terms: Iterable[str]) -> dict[str, str]:
        return {t: self._lower[t.lower()] for t in terms if t.lower() in self._lower}
