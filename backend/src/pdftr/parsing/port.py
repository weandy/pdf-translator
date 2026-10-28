"""版面解析端口。

解析服务（MinerU 云端、Doc2X、本地 MinerU）各自实现 LayoutParser，
输出统一的 LayoutResult；融合模块只认 LayoutResult，不关心来自哪个服务。
"""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType


class LayoutSpan(BaseModel):
    kind: str  # "text" / "inline_formula"
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
    discarded: bool = False  # 解析器认为无需展示的块（页眉、页码等）


class LayoutPage(BaseModel):
    index: int
    width: float
    height: float
    blocks: list[LayoutBlock] = Field(default_factory=list)


class LayoutResult(BaseModel):
    parser: str
    parser_version: str = ""
    pages: list[LayoutPage]


class LayoutParser(Protocol):
    name: str
    is_cloud: bool  # 为真时，前端必须取得用户的上传确认

    async def parse(self, pdf: Path, pages: list[int] | None = None) -> LayoutResult: ...
