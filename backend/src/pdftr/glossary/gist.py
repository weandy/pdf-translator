"""GIST 多语言 AI 术语数据集（中文部分 6426 条，Apache-2.0）。

来源：Liu et al., "Towards Global AI Inclusivity: A Large-Scale Multilingual Terminology Dataset (GIST)",
https://huggingface.co/datasets/Jerry999/multilingual-terminology
下载与缓存见 glossary/sources.py。
"""

import csv
import io

URL = "https://huggingface.co/datasets/Jerry999/multilingual-terminology/resolve/main/Chinese.csv"


def parse(text: str) -> dict[str, str]:
    """解析 CSV（列：Index, English, Chinese）。同一英文词条出现多次时保留第一条。"""
    terms: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text)):
        en, zh = (row.get("English") or "").strip(), (row.get("Chinese") or "").strip()
        if en and zh:
            terms.setdefault(en, zh)
    return terms
