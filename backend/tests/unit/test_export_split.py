"""导出用的译文拆分与前端 shared/text/split.ts 行为一致（用例与 split.test.ts 相同）。"""

import re

from pdftr.rendering.export import split_by_parts


def test_single_part_not_split():
    assert split_by_parts("全部译文", [10]) == ["全部译文"]


def test_split_at_punctuation_by_source_ratio():
    text = "第一部分的内容比较长，一直写到这里。第二部分短。"
    parts = split_by_parts(text, [30, 10])
    assert len(parts) == 2 and "".join(parts) == text
    assert parts[0].endswith(("。", "，"))


def test_placeholder_never_split():
    text = "甲" * 9 + "<m12/>" + "乙" * 9
    for parts in (split_by_parts(text, [10, 10]), split_by_parts(text, [11, 9])):
        assert "".join(parts) == text
        assert all(not re.search(r"<m\d*$", p) for p in parts)


def test_three_parts_all_non_empty():
    text = "一，二，三，四，五，六，七，八，九。"
    parts = split_by_parts(text, [6, 6, 6])
    assert len(parts) == 3 and "".join(parts) == text and all(parts)
