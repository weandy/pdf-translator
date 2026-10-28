"""译文排版规则（clreq）：段首缩进与前端 shared/text/typeset.test.ts 用例一致。"""

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Segment
from pdftr.rendering.typeset import wants_indent


def _block(btype: BlockType = BlockType.TEXT) -> Block:
    return Block(id="p0-b0", page=0, type=btype, bbox=Rect.of([0, 0, 100, 10]), order=0,
                 style=BlockStyle(font_size=10), translatable=True)  # fmt: skip


def _seg(source: str) -> Segment:
    return Segment(id="s", job_id="0123456789abcdef", block_ids=["p0-b0"], source=source)


def test_indent_rules():
    assert wants_indent(_block(), _seg("We propose a method."), first=True)
    assert not wants_indent(_block(), _seg("We propose a method."), first=False)  # 跨栏的后半段
    assert not wants_indent(_block(), _seg("where x is the input."), first=True)  # 公式后接着说
    assert not wants_indent(_block(), _seg("• First item"), first=True)
    assert not wants_indent(_block(), _seg("(1) First item"), first=True)
    assert not wants_indent(_block(BlockType.CAPTION), _seg("Figure 1: A caption."), first=True)
