"""中间表示（IR）：描述“文档是什么”，不含任何服务调用细节。

各能力模块之间只通过这些结构交换数据（见 docs/03-mvp-design.md §3.2）。
IR 在解析完成后不再改动；译文单独存放在段落（Segment）里。
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from pdftr.domain.geometry import Rect


class PageKind(StrEnum):
    NATIVE = "native"  # 原生文本
    SCANNED = "scanned"  # 扫描页
    GARBLED = "garbled"  # 文字提取乱码（如缺 ToUnicode 的 CID 字体）


class BlockType(StrEnum):
    TEXT = "text"
    TITLE = "title"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    FORMULA = "formula"  # 独立公式
    TABLE = "table"
    FIGURE = "figure"
    CODE = "code"  # 代码、算法框
    HEADER = "header"
    FOOTER = "footer"
    REFERENCE = "reference"


# 默认参与翻译的块类型；参考文献是否翻译由任务选项决定
TRANSLATABLE_TYPES = frozenset({BlockType.TEXT, BlockType.TITLE, BlockType.CAPTION, BlockType.FOOTNOTE})


class Span(BaseModel):
    """块内按阅读顺序排列的片段：文字，或行内公式的位置（内容见 Block.formulas）。"""

    kind: Literal["text", "formula"] = "text"
    text: str = ""  # kind=formula 时为空
    formula: str | None = None  # kind=formula 时为公式 key，如 "m1"
    line: int = 0  # 块内行号，用于拼接时判断换行与断词
    bbox: Rect
    font: str | None = None
    size: float
    color: int = 0
    bold: bool = False
    italic: bool = False


class InlineFormula(BaseModel):
    key: str  # 占位符编号，如 "m1"
    latex: str
    bbox: Rect  # KaTeX 渲染失败时用于原图裁剪


class BlockStyle(BaseModel):
    font_size: float
    bold: bool = False
    italic: bool = False
    serif: bool = True
    color: int = 0


class Block(BaseModel):
    id: str  # 如 "p3-b12"
    page: int
    type: BlockType
    bbox: Rect
    order: int  # 阅读顺序
    spans: list[Span] = Field(default_factory=list)
    formulas: list[InlineFormula] = Field(default_factory=list)
    latex: str | None = None  # 独立公式的 LaTeX
    # 跨栏/跨页段落：本块接在哪个块后面（同一段落的前一部分）。
    # 段落切分时沿这条链合并成一个翻译单元，渲染时按各块原文长度拆分译文。
    continues_from: str | None = None
    style: BlockStyle
    translatable: bool
    orphan: bool = False  # 解析器漏掉、由未归属字符补出来的块


class Page(BaseModel):
    index: int
    width: float
    height: float
    rotation: int = 0
    kind: PageKind = PageKind.NATIVE
    blocks: list[Block] = Field(default_factory=list)


class DocMeta(BaseModel):
    title: str = ""
    abstract: str = ""
    domain: str = ""  # 领域一句话（术语抽取时得到）
    source_lang: str = "en"
    parser: str = ""
    used_cloud: bool = False


class Document(BaseModel):
    job_id: str
    pages: list[Page]
    meta: DocMeta = Field(default_factory=DocMeta)

    def blocks(self) -> list[Block]:
        return [b for page in self.pages for b in page.blocks]

    def block(self, block_id: str) -> Block | None:
        for page in self.pages:
            for b in page.blocks:
                if b.id == block_id:
                    return b
        return None


class SegmentStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    EDITED = "edited"  # 用户手动修改过，后续重翻不覆盖


Quality = Literal["default", "careful"]  # careful = 精翻（开启低强度思考）


class SegmentPart(BaseModel):
    """段落在某个块里的那一部分：渲染时按各部分原文长度拆分译文。"""

    block_id: str
    chars: int  # 这部分原文的字符数


class FormulaRef(BaseModel):
    """段落内公式占位符对应的原文位置（KaTeX 渲染失败时按位置裁剪原图）。"""

    page: int
    bbox: Rect


class Segment(BaseModel):
    """翻译单元：一个段落，跨页时对应多个块。"""

    id: str
    job_id: str
    block_ids: list[str]  # 跨栏/跨页时按 continues_from 链的顺序排列
    source: str  # 含 <mN/> 占位符
    formulas: dict[str, str] = Field(default_factory=dict)  # 占位符 key → LaTeX
    formula_refs: dict[str, FormulaRef] = Field(default_factory=dict)
    parts: list[SegmentPart] = Field(default_factory=list)
    section: str = ""  # 所在章节，作为批次上下文
    target: str | None = None
    status: SegmentStatus = SegmentStatus.PENDING
    quality: Quality = "default"
