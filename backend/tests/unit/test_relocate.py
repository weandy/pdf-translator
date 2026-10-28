import json
from pathlib import Path

from pdftr.fusion.relocate import relocate
from pdftr.parsing.mineru_normalize import normalize

FIXTURE = Path(__file__).parent.parent / "fixtures" / "mineru_layout_min.json"


def test_continuation_lines_move_to_placeholders():
    layout = normalize(json.loads(FIXTURE.read_text(encoding="utf-8")))
    moved, report = relocate(layout)
    p0, p1 = moved.pages
    head, same_page = p0.blocks[1], p0.blocks[2]
    next_page = p1.blocks[0]

    # 原块只剩自己框内的那一行
    assert head.lines[0].spans[0].content == "Let"
    assert len(head.lines) == 1
    # 跨栏的行搬到同页占位块，跨页的行搬到下一页占位块
    assert same_page.lines[0].spans[0].content == "continues in the next column"
    assert next_page.lines[0].spans[0].content == "and ends on the next page."
    assert not same_page.placeholder and not next_page.placeholder

    # 链：p0 第 1 块 → p0 第 2 块 → p1 第 0 块
    assert report.links == {(0, 2): (0, 1), (1, 0): (0, 2)}
    assert (report.moved_lines, report.unmatched_lines) == (2, 0)
    # 不修改输入
    assert len(layout.pages[0].blocks[1].lines) == 3
