"""版面解析结果：各解析服务（MinerU、Doc2X、本地）统一输出的结构。

放在 domain 是因为它要在 parsing（产出）和 fusion（消费）之间传递，
而能力模块之间不能互相导入（docs/03-mvp-design.md §2.1）。
"""

from typing import Literal

from pydantic import BaseModel, Field

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType


class LayoutSpan(BaseModel):
    kind: Literal["text", "inline_formula"]
    content: str  # 文字或 LaTeX
    bbox: Rect


class LayoutLine(BaseModel):
    bbox: Rect
    spans: list[LayoutSpan] = Field(default_factory=list)


class LayoutBlock(BaseModel):
    type: BlockType
    bbox: Rect  # PDF point
    order: int
    lines: list[LayoutLine] = Field(default_factory=list)
    latex: str | None = None  # 独立公式
    level: int | None = None  # 标题层级
    merge_prev: bool = False  # 与上一块是同一段落（跨栏/跨页）
    # 续行占位块：解析器把这块的行并进了前面的块，这里只剩位置（MinerU 的 lines_deleted）
    placeholder: bool = False
    discarded: bool = False  # 解析器认为无需展示的块（页眉、页码等）
    source_type: str = ""  # 解析器原始类型名，便于排查
    # 块级解析器（如 PaddleOCR-VL）只给块坐标与内容，行内公式以 $LaTeX$ 标出、没有位置；
    # 此时 lines 为空，由融合阶段对照 PDF 字符拆成行与片段并推算公式位置（fusion/formula_align.py）
    content: str | None = None


class LayoutPage(BaseModel):
    index: int
    width: float
    height: float
    blocks: list[LayoutBlock] = Field(default_factory=list)


class LayoutResult(BaseModel):
    parser: str
    parser_version: str = ""
    pages: list[LayoutPage]
