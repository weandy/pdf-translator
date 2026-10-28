"""导出字体：按 GB/T 7713.2 分配字体角色，中文与西文分开包装（MuPDF 不会按字体列表逐字回退）。"""

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType
from pdftr.rendering.export import CAPTION_NO, role
from pdftr.rendering.fonts import wrap_cjk


def _block(btype: BlockType, bold: bool = False) -> Block:
    return Block(id="p0-b0", page=0, type=btype, bbox=Rect.of([0, 0, 100, 10]), order=0,
                 style=BlockStyle(font_size=10, bold=bold), translatable=True)  # fmt: skip


def test_wrap_cjk_separates_scripts():
    assert wrap_cjk("使用 Transformer 模型，“注意力”β1") == (
        '<span class="zh">使用</span> Transformer <span class="zh">模型，“注意力”</span>β1'
    )


def test_roles_follow_gbt_7713():
    assert role(_block(BlockType.TITLE)) == "heading"  # 标题：黑体
    assert role(_block(BlockType.TEXT, bold=True)) == "heading"
    assert role(_block(BlockType.CAPTION)) == "caption"  # 编号黑体，其余宋体
    assert role(_block(BlockType.TEXT)) == "body"  # 正文：宋体


def test_caption_number():
    assert CAPTION_NO.match("图 4. 模型性能")[0] == "图 4."  # type: ignore[index]
    assert CAPTION_NO.match("表2：结果")[0] == "表2："  # type: ignore[index]
    assert CAPTION_NO.match("如图 4 所示") is None
