"""验证实验 S4：MinerU 版面块 ↔ PyMuPDF 字符对齐。

把 PyMuPDF 提取的每个字符按中心点归属到 MinerU 的块（layout.json，PDF point 坐标），统计：
- 覆盖率：多少字符能归到某个块，按块类型细分；没归属的字符是什么
- 文字一致性：同一块里 MinerU 的文字与 PyMuPDF 的文字是否一致（验证坐标系）
- 行内公式：落在 inline_equation 框里的字符数，及有多少正文字符被误框进公式

用法：python s4_align.py samples/1412.6980.pdf out/mineru/1412.6980/vlm/layout.json
"""

import collections
import difflib
import json
import re
import sys
from pathlib import Path

import pymupdf

TOL = 1.5  # 框外扩容差（pt）


def flat_blocks(page_info):
    """展开 MinerU 的块：带子块的（图、表、代码）取子块；返回 (类型, bbox, 块对象, 是否丢弃区)。"""
    out = []
    for b in page_info["para_blocks"]:
        for x in b.get("blocks", [b]):
            out.append((x["type"], x["bbox"], x, False))
    for b in page_info["discarded_blocks"]:
        out.append((b["type"], b["bbox"], b, True))
    return out


def inside(pt, bbox, tol=TOL):
    x, y = pt
    return bbox[0] - tol <= x <= bbox[2] + tol and bbox[1] - tol <= y <= bbox[3] + tol


def area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def norm(s: str) -> str:
    return re.sub(r"[\s\-‐]", "", s).lower()


def mineru_text(block) -> str:
    return "".join(
        s.get("content", "") for line in block.get("lines", []) for s in line.get("spans", []) if s["type"] == "text"
    )


def main(pdf_path: str, layout_path: str):
    doc = pymupdf.open(pdf_path)
    layout = json.loads(Path(layout_path).read_text(encoding="utf-8"))
    total = 0
    by_type = collections.Counter()
    unassigned_samples = collections.Counter()
    multi = 0
    sims = []
    eq_chars = 0
    eq_text_chars = 0  # 落在行内公式框里、但字体不像数学字体的字符（可能误框）
    worst = []

    for pno, info in enumerate(layout["pdf_info"]):
        page = doc[pno]
        blocks = flat_blocks(info)
        per_block_text = collections.defaultdict(list)
        inline_eqs = [
            s["bbox"]
            for _, _, b, _ in blocks
            for line in b.get("lines", [])
            for s in line.get("spans", [])
            if s["type"] == "inline_equation"
        ]
        for blk in page.get_text("rawdict")["blocks"]:
            for line in blk.get("lines", []):
                for span in line["spans"]:
                    for ch in span["chars"]:
                        c = ch["c"]
                        if not c.strip():
                            continue
                        total += 1
                        x0, y0, x1, y1 = ch["bbox"]
                        center = ((x0 + x1) / 2, (y0 + y1) / 2)
                        hits = [(area(bb), i) for i, (_, bb, _, _) in enumerate(blocks) if inside(center, bb)]
                        if not hits:
                            unassigned_samples[c] += 1
                            by_type["<未归属>"] += 1
                            continue
                        if len(hits) > 1:
                            multi += 1
                        _, i = min(hits)
                        by_type[blocks[i][0]] += 1
                        per_block_text[i].append(c)
                        if any(inside(center, e, 0.5) for e in inline_eqs):
                            eq_chars += 1
                            if not re.search(r"CM(MI|SY|EX|R)|MS[AB]M|Math|Symbol|STIX|rsfs|eufm", span["font"], re.I) \
                                    and re.match(r"[A-Za-z]", c) and len(span["chars"]) > 6:
                                eq_text_chars += 1

        for i, chars in per_block_text.items():
            typ, _, b, _ = blocks[i]
            if typ not in ("text", "title", "ref_text", "chart_caption", "image_caption", "table_caption"):
                continue
            m = norm(mineru_text(b))
            p = norm("".join(chars))
            if len(m) < 20:
                continue
            # PyMuPDF 一侧包含行内公式字符，MinerU 一侧不含，用"MinerU 文字有多少能在 PyMuPDF 里按顺序找到"衡量
            sm = difflib.SequenceMatcher(None, m, p, autojunk=False)
            matched = sum(t.size for t in sm.get_matching_blocks())
            r = matched / len(m)
            sims.append(r)
            worst.append((round(r, 3), pno, typ, m[:60], p[:60]))

    worst.sort()
    assigned = total - by_type["<未归属>"]
    report = {
        "pdf": Path(pdf_path).name,
        "chars": total,
        "assigned_pct": round(assigned / total * 100, 2),
        "multi_hit_pct": round(multi / total * 100, 2),
        "by_type": dict(by_type.most_common()),
        "unassigned_top": unassigned_samples.most_common(15),
        "text_blocks_compared": len(sims),
        "text_match_mean": round(sum(sims) / len(sims), 4) if sims else None,
        "text_match_lt_0.95": sum(1 for s in sims if s < 0.95),
        "inline_eq_chars": eq_chars,
        "inline_eq_suspect_text_chars": eq_text_chars,
        "worst5": worst[:5],
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:])
