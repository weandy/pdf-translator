"""融合回归：5 篇真实论文（S4 实验的结论作为基线）。

样本不入库，由 scripts/fetch_samples.py 生成到 backend/.samples/（需要 MinerU Token）；
样本缺失时跳过，所以 CI 中不运行。
"""

import json
import os
from pathlib import Path

import pytest

from pdftr.extraction.chars import extract
from pdftr.fusion.build import fuse
from pdftr.parsing.mineru_normalize import normalize

SAMPLES = Path(os.environ.get("PDFTR_SAMPLES_DIR", Path(__file__).parents[2] / ".samples"))

# 论文 → (页数, 跨栏/跨页续接数, 行内公式数, 标题开头)；数值来自 M2 首次运行，续接数与 S4 人工核对一致
BASELINE = {
    "1412.6980": (15, 6, 255, "ADAM: A METHOD"),
    "1512.03385": (12, 10, 26, "Deep Residual Learning"),
    "1706.03762": (15, 5, 54, "Attention Is All You Need"),
    "1810.04805": (16, 13, 30, "BERT: Pre-training"),
    "2605.10845": (10, 10, 0, "BabelDOC"),
}


@pytest.mark.parametrize("paper", sorted(BASELINE))
def test_paper(paper: str):
    pdf, layout_file = SAMPLES / f"{paper}.pdf", SAMPLES / f"{paper}.layout.json"
    if not (pdf.exists() and layout_file.exists()):
        pytest.skip(f"缺少样本 {paper}，运行 scripts/fetch_samples.py 生成")
    pages, continuations, formulas, title = BASELINE[paper]
    doc, report = fuse(
        "0123456789abcdef", normalize(json.loads(layout_file.read_text())), extract(pdf), used_cloud=True
    )

    assert report.pages == pages
    assert report.coverage >= 0.995  # S4：99.84%–100%
    assert report.unmatched_lines == 0
    assert report.continuations == continuations
    assert report.formulas == formulas
    assert doc.meta.title.startswith(title)
    assert len(doc.meta.abstract.split()) >= 60
    assert not report.non_native_pages
    # 续接链的每一环都指向存在的块，且没有环
    for b in doc.blocks():
        seen = set()
        cur = b
        while cur.continues_from:
            assert cur.id not in seen
            seen.add(cur.id)
            nxt = doc.block(cur.continues_from)
            assert nxt is not None
            cur = nxt
