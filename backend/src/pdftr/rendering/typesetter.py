"""导出 PDF 的中文排版器：按 W3C《中文排版需求》（clreq）断行、对齐，直接写字形，不经过 HTML 排版。

为什么不用 MuPDF 的 HTML 排版（Story）：它两端对齐时只拉宽空格，中英混排的行里空格很少，
剩余空间全压在一两个空格上（如“LLM　　能够”）；汉字之间的空隙它不分配。这里自己断行：
- 两端对齐（§6.2）：剩余空间均分到所有可伸缩处（汉字之间、空格、中西文间距），段落末行不拉伸；
- 中西文间距（§6.3.3）：四分之一个汉字宽，作为排版间距处理，不插入空格字符（复制出来的文字不多空格）；
- 行首行尾禁则（§6.1.1）：点号、结束括号引号不出现在行首（悬挂在上一行行尾），开始括号引号不出现在行尾；
- 段首缩进两字（§6.2.1.1）、行距由调用方给定；上下标缩小并升降，行内公式以图片排入。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from pdftr.rendering import fonts

_HAN = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CJK = fonts.CJK_RUN  # 汉字与全角标点、中文引号等：逐字断行，用中文字体
# 不能出现在行首的符号（悬挂到上一行行尾）；不能出现在行尾的符号（移到下一行行首）
_NO_START = set("，。、；：？！）》」』】〕〉”’…—,.;:?!)]}%")
_NO_END = set("（《「『【〔〈“‘([{")
_TOKEN = re.compile(r"<(/?)(sup|sub|b|i)>|<(m\d+)/>")
_SCRIPT_SCALE = 0.7  # 上下标字号
_WORD_BREAK = re.compile(r"(?<=[A-Za-z0-9][/\-–])(?=[A-Za-z0-9])")  # 词内可断行处：/ 与连字符之后


@dataclass
class FontSet:
    """排版用到的字体：按角色取中文、西文字体；缺字时退到 MuPDF 内置的中文字体。"""

    song: pymupdf.Font
    hei: pymupdf.Font
    times: pymupdf.Font
    times_bold: pymupdf.Font
    fallback: pymupdf.Font = field(default_factory=lambda: pymupdf.Font("cjk"))

    @classmethod
    def load(cls, font_dir: Path | None) -> "FontSet":
        if font_dir is not None and fonts.available(font_dir):
            f = fonts.FILES
            return cls(*(pymupdf.Font(fontfile=str(font_dir / f[k])) for k in ("song", "hei", "times", "times-bold")))
        cjk = pymupdf.Font("cjk")  # 没有字体文件时（开发环境）用内置字体，排版规则不变
        return cls(cjk, cjk, pymupdf.Font("tiro"), pymupdf.Font("tibo"), cjk)


@dataclass
class Style:
    """一段文字的样式：role 为 heading/caption/body/label，no 表示图表编号，script 为上下标。"""

    role: str
    bold: bool = False
    no: bool = False
    script: str = ""  # "sup" / "sub" / ""


@dataclass
class Run:
    """同一字体、同一字号的一段字形。"""

    text: str
    font: pymupdf.Font
    size: float
    rise: float  # 基线上移量（上标为正）
    width: float


@dataclass
class Token:
    """断行的最小单位：一个汉字或全角标点、一个西文词、一个空格、一张公式图。"""

    kind: str  # "cjk" / "word" / "space" / "image"
    runs: list[Run] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0  # 图片高度
    image: str = ""  # 公式占位符
    char: str = ""  # cjk 的字，用于禁则判断
    attach: bool = False  # 紧接上一个 token（同一个词在 / 或连字符后拆开的部分）：不加间距、不拉伸，但可在此断行


@dataclass
class Placed:
    """画在页面上的一段文字：起点（基线）与字体，用来算其中某几个字的位置。"""

    text: str
    x: float
    baseline: float
    font: pymupdf.Font
    size: float

    def span(self, start: int, end: int) -> pymupdf.Rect:
        """第 start 到 end 个字（不含 end）占的矩形。"""
        x0 = self.x + self.font.text_length(self.text[:start], fontsize=self.size)  # pyright: ignore[reportArgumentType]
        x1 = self.x + self.font.text_length(self.text[:end], fontsize=self.size)  # pyright: ignore[reportArgumentType]
        return pymupdf.Rect(x0, self.baseline - self.size * 0.9, x1, self.baseline + self.size * 0.25)


@dataclass
class Line:
    tokens: list[Token]
    glue: list[float]  # 每个 token 之前的固定间距（中西文间距），与 tokens 等长
    last: bool = False  # 段落末行：不拉伸


ImageSize = Callable[[str], tuple[float, float] | None]


class Typesetter:
    def __init__(self, font_set: FontSet) -> None:
        self.f = font_set

    # ---- 字体选择 ----
    def _font_for(self, ch: str, style: Style) -> pymupdf.Font:
        cjk = bool(_CJK.fullmatch(ch))
        if style.role == "label":
            first = self.f.hei
        elif cjk:
            first = self.f.hei if style.role == "heading" or style.no else self.f.song
        else:
            first = self.f.times_bold if style.role == "heading" or style.no or style.bold else self.f.times
        for font in (first, self.f.song, self.f.times, self.f.fallback):
            if font.has_glyph(ord(ch)):
                return font
        return self.f.fallback

    def _runs(self, text: str, style: Style, size: float) -> list[Run]:
        if style.script:
            size, rise = size * _SCRIPT_SCALE, (0.35 if style.script == "sup" else -0.15) * size
        else:
            rise = 0.0
        runs: list[Run] = []
        for ch in text:
            font = self._font_for(ch, style)
            if runs and runs[-1].font is font:
                runs[-1].text += ch
            else:
                runs.append(Run(ch, font, size, rise, 0.0))
        for r in runs:
            r.width = r.font.text_length(r.text, fontsize=r.size)  # pyright: ignore[reportArgumentType]  字号可为小数
        return runs

    # ---- 切分 ----
    def tokenize(self, text: str, role: str, size: float, image_size: ImageSize, caption_no: str = "") -> list[Token]:
        """把译文（含 <sup>/<sub>/<b>/<i> 与 <mN/> 占位符）切成 token。caption_no 为开头的图表编号（黑体）。"""
        tokens: list[Token] = []
        stack: list[str] = []
        pos = 0
        if caption_no and text.startswith(caption_no):
            tokens += self._text_tokens(caption_no, Style(role, no=True), size)
            pos = len(caption_no)
        for m in _TOKEN.finditer(text, pos):
            tokens += self._text_tokens(text[pos : m.start()], self._style(role, stack), size)
            pos = m.end()
            if m.group(3):
                wh = image_size(m.group(3))
                if wh:
                    tokens.append(Token("image", width=wh[0], height=wh[1], image=m.group(3)))
            elif m.group(1):
                if m.group(2) in stack:
                    stack.remove(m.group(2))
            else:
                stack.append(m.group(2))
        tokens += self._text_tokens(text[pos:], self._style(role, stack), size)
        return tokens

    @staticmethod
    def _style(role: str, stack: list[str]) -> Style:
        script = next((t for t in reversed(stack) if t in ("sup", "sub")), "")
        return Style(role, bold="b" in stack, script=script)

    def _text_tokens(self, text: str, style: Style, size: float) -> list[Token]:
        out: list[Token] = []
        word = ""

        def flush() -> None:
            nonlocal word
            # 长词（如 Python/OR-Tools）允许在 / 与连字符后断行，否则整行放不下时上一行字距会被拉得很开
            for i, piece in enumerate(p for p in _WORD_BREAK.split(word) if p):
                runs = self._runs(piece, style, size)
                out.append(Token("word", runs, sum(r.width for r in runs), attach=i > 0))
            word = ""

        for ch in text:
            if ch.isspace():
                flush()
                if out and out[-1].kind != "space":
                    runs = self._runs(" ", style, size)
                    out.append(Token("space", runs, sum(r.width for r in runs)))
            elif _CJK.fullmatch(ch):
                flush()
                runs = self._runs(ch, style, size)
                out.append(Token("cjk", runs, sum(r.width for r in runs), char=ch))
            else:
                word += ch
        flush()
        return out

    # ---- 断行 ----
    @staticmethod
    def _glue_before(prev: Token | None, tok: Token, size: float) -> float:
        """汉字与西文、数字、公式之间四分之一个汉字宽（标点旁不加，已有空格时不加）。"""
        if prev is None or prev.kind == "space" or tok.kind == "space" or tok.attach:
            return 0.0
        han = lambda t: t.kind == "cjk" and bool(_HAN.fullmatch(t.char))  # noqa: E731
        latin = lambda t: t.kind in ("word", "image")  # noqa: E731
        return size / 4 if (han(prev) and latin(tok)) or (latin(prev) and han(tok)) else 0.0

    def break_lines(self, tokens: list[Token], width: float, size: float, indent: float) -> list[Line]:
        lines: list[Line] = []
        cur: list[Token] = []
        glue: list[float] = []
        used = indent
        for tok in tokens:
            g = self._glue_before(cur[-1] if cur else None, tok, size)
            if tok.kind == "space" and not cur:
                continue  # 行首空格丢弃
            fits = used + g + tok.width <= width + 0.01
            hang = tok.kind == "cjk" and tok.char in _NO_START and cur  # 行首禁则：悬挂在本行行尾
            if fits or hang or not cur:
                cur.append(tok)
                glue.append(g)
                used += g + tok.width
                continue
            carry: list[Token] = []
            while cur and cur[-1].kind == "space":  # 行尾空格不计
                cur.pop()
                glue.pop()
            if cur and cur[-1].kind == "cjk" and cur[-1].char in _NO_END and len(cur) > 1:
                carry.append(cur.pop())  # 行尾禁则：开始括号、引号移到下一行
                glue.pop()
            lines.append(Line(cur, glue))
            cur, glue, used = [], [], 0.0
            for t in [*carry, tok]:
                if t.kind == "space" and not cur:
                    continue
                g = self._glue_before(cur[-1] if cur else None, t, size)
                cur.append(t)
                glue.append(g)
                used += g + t.width
        while cur and cur[-1].kind == "space":
            cur.pop()
            glue.pop()
        if cur:
            lines.append(Line(cur, glue, last=True))
        elif lines:
            lines[-1].last = True
        return lines

    # ---- 排版与绘制 ----
    def layout(
        self, tokens: list[Token], width: float, size: float, line_height: float, indent: bool
    ) -> tuple[list[Line], list[float], float]:
        """返回（行，各行基线位置，总高度）。行高取 line_height 倍字号与本行最高的公式图中较大者。"""
        lines = self.break_lines(tokens, width, size, 2 * size if indent else 0.0)
        baselines: list[float] = []
        y = 0.0
        for line in lines:
            img = max((t.height for t in line.tokens if t.kind == "image"), default=0.0)
            pitch = max(size * line_height, img + size * (line_height - 1))
            ascent = size * 0.88 + (pitch - size * line_height) + (line_height - 1) * size / 2
            baselines.append(y + ascent)
            y += pitch
        return lines, baselines, y

    def draw(
        self,
        page: pymupdf.Page,
        writer: pymupdf.TextWriter,
        origin: tuple[float, float],
        width: float,
        lines: list[Line],
        baselines: list[float],
        size: float,
        align: str,
        indent: bool,
        image_bytes: Callable[[str], bytes | None],
        scale: float = 1.0,
        placed: list[Placed] | None = None,
    ) -> None:
        """把排好的行写到页面上。scale < 1 时整体缩小（放不下时缩小字号）。

        placed 不为 None 时，按书写顺序记下每段文字画在页面上的位置（给译文里的链接定位用）。
        """
        x0, y0 = origin
        for n, (line, base) in enumerate(zip(lines, baselines, strict=True)):
            lead = 2 * size if indent and n == 0 else 0.0
            natural = lead + sum(line.glue) + sum(t.width for t in line.tokens)
            extra, offset = 0.0, 0.0
            gaps = sum(1 for t in line.tokens[1:] if not t.attach)
            if align == "justify" and not line.last and gaps > 0:
                extra = max(width - natural, 0.0) / gaps
            elif align == "center":
                offset = max(width - natural, 0.0) / 2
            x = lead + offset
            for i, tok in enumerate(line.tokens):
                x += line.glue[i] + (extra if i > 0 and not tok.attach else 0.0)
                if tok.kind == "image":
                    data = image_bytes(tok.image)
                    if data:
                        top = base + size * 0.15 - tok.height  # 底边略低于基线，与文字底部对齐
                        rect = pymupdf.Rect(x, top, x + tok.width, top + tok.height)
                        page.insert_image(_scaled(rect, scale, x0, y0), stream=data)
                else:
                    rx = x
                    for run in tok.runs:
                        point = pymupdf.Point(x0 + rx * scale, y0 + (base - run.rise) * scale)
                        writer.append(point, run.text, font=run.font, fontsize=run.size * scale)  # pyright: ignore[reportArgumentType]
                        if placed is not None:
                            placed.append(Placed(run.text, point.x, point.y, run.font, run.size * scale))
                        rx += run.width
                x += tok.width


def _scaled(rect: pymupdf.Rect, scale: float, x0: float, y0: float) -> pymupdf.Rect:
    return pymupdf.Rect(x0 + rect.x0 * scale, y0 + rect.y0 * scale, x0 + rect.x1 * scale, y0 + rect.y1 * scale)
