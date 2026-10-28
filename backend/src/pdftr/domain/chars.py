"""字符级数据：由 extraction 产出、fusion 消费。"""

from pydantic import BaseModel, Field

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import PageKind


class Char(BaseModel):
    c: str
    bbox: Rect
    font: str
    size: float
    color: int = 0
    bold: bool = False
    italic: bool = False
    line: int  # 所在 PyMuPDF 行的全页序号，用于按行重建文字
    upright: bool = True  # 所在行是否水平从左到右（图里竖排、斜排的标签不翻译）


class CharPage(BaseModel):
    index: int
    width: float
    height: float
    rotation: int = 0
    kind: PageKind = PageKind.NATIVE
    chars: list[Char] = Field(default_factory=list)
