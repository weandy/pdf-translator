"""跨栏/跨页段落的续行归位（S4）。

MinerU 把同一段落的后续行并进前一块（这些行的坐标仍是它们实际所在的位置），
原位置留下占位块。这里把块框之外的行按阅读顺序搬回占位块：
先找同页、排在后面的占位块，再找下一页的占位块。
搬回后，每个块的文字都位于自己的坐标范围内，并用 continues_from 串成链。
"""

from dataclasses import dataclass, field

from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutResult

TOL = 3.0  # 行中心点落在块框外扩 3pt 内即视为属于该块


@dataclass
class RelocationReport:
    moved_lines: int = 0
    unmatched_lines: int = 0
    # (页, 块序号) → 它接续的 (页, 块序号)
    links: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)


def _inside(line: LayoutLine, block: LayoutBlock) -> bool:
    x, y = line.bbox.center
    return block.bbox.contains_point(x, y, TOL)


def relocate(layout: LayoutResult) -> tuple[LayoutResult, RelocationReport]:
    result = layout.model_copy(deep=True)
    report = RelocationReport()
    pages = result.pages
    for pi, page in enumerate(pages):
        for block in page.blocks:
            if block.placeholder or not block.lines:
                continue
            outside = [ln for ln in block.lines if not _inside(ln, block)]
            if not outside:
                continue
            candidates: list[tuple[int, LayoutBlock]] = [
                (pi, b) for b in page.blocks if b.placeholder and b.order > block.order
            ]
            if pi + 1 < len(pages):
                candidates += [(pi + 1, b) for b in pages[pi + 1].blocks if b.placeholder]
            stays = {id(ln) for ln in block.lines if _inside(ln, block)}
            prev = (pi, block.order)
            cursor = 0
            for line in outside:
                # 按阅读顺序向后找第一个能容纳该行的占位块
                k = next((i for i in range(cursor, len(candidates)) if _inside(line, candidates[i][1])), None)
                if k is None:
                    stays.add(id(line))  # 找不到归属就留在原块，宁可错位也不丢字
                    report.unmatched_lines += 1
                    continue
                cursor = k
                target_page, target = candidates[k]
                if not target.lines:
                    report.links[(target_page, target.order)] = prev
                    prev = (target_page, target.order)
                target.lines.append(line)
                report.moved_lines += 1
            block.lines = [ln for ln in block.lines if id(ln) in stays]
    for page in pages:
        for b in page.blocks:
            if b.placeholder and b.lines:
                b.placeholder = False
    return result, report
