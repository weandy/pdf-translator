"""导出 PDF：在去字背景页上排入译文（纯译文），或与原文左右并排（双语对照）。

只用 PyMuPDF，不需要浏览器，内存占用小。译文由 rendering/typesetter.py 按《中文排版需求》排版后直接写入字形；
行内公式用原文裁图，缺少位置时排入 LaTeX；排不下时先收紧行距，再缩小字号。
字体按 GB/T 7713.2 附录 B：正文宋体、标题黑体、图表编号黑体，西文用 Times 类字体（见 rendering/fonts.py），
与网页阅读器一致；按用到的字形子集嵌入。
"""

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pymupdf

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockType, Document, FormulaRef, Segment, SegmentPart
from pdftr.domain.text import RICH_ATOM, RICH_TAG
from pdftr.rendering.artifacts import pdf_destination
from pdftr.rendering.background import crop_formula
from pdftr.rendering.links import SKIP_DESTS, PdfLink, extract_links, locate
from pdftr.rendering.typeset import LINE_HEIGHTS, drop_fake_scripts, normalize_cjk_spacing, wants_indent
from pdftr.rendering.typesetter import _NO_START, FontSet, Line, Placed, Token, Typesetter

log = logging.getLogger(__name__)

ExportMode = Literal["mono", "dual"]
Side = Literal["left", "right"]  # 双语对照时译文放在哪一边
LINE_HEIGHT = 1.2  # 最紧的行距（单行框补高按它计算）
MIN_SCALE = 0.4  # 常规适配的最小字号比例，极矮框的首行可继续缩小
BOTTOM_MARGIN = 4.0

_BREAK_AFTER = set("，。；：、）！？,;:.)")
_STRONG_BREAK_AFTER = set("。！？；.!?;")
_SEARCH = 40
_CITATION_MAX = 40
_CITATION_CHARS = set(" ,–—-;，、；")
_ASCII_DIGITS = set("0123456789")
_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")
_URL_CHAR = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]")
_URL_TAIL = set(".,;:!?")
_CLOSING_TAGS = re.compile(r"(?:</(?:b|i|sup|sub)>)*")
# 图表标题开头的编号（“图 4.”“表2：”），按 GB/T 7713.2 用黑体（与前端 entities/document/model.ts 一致）
CAPTION_NO = re.compile(r"^\s*(?:图|表)\s*[A-Z]?\d+(?:\.\d+)*[.:：．。]?")


@dataclass(frozen=True, slots=True)
class _Atom:
    start: int
    end: int
    char: str  # 仅单单位原子参与标点与行首禁则判断


def _citation_end(chars: list[str], start: int) -> int:
    close = "]" if chars[start] == "[" else "］"
    has_digit = False
    for end in range(start + 1, min(len(chars), start + _CITATION_MAX)):
        char = chars[end]
        if char == close:
            return end + 1 if has_digit else start
        if char in _ASCII_DIGITS:
            has_digit = True
        elif char not in _CITATION_CHARS:
            break
    return start


def _url_end(chars: list[str], start: int) -> int:
    if start and _ASCII_ALNUM.fullmatch(chars[start - 1]):
        return start
    prefix = "".join(chars[start : start + 8]).lower()
    if prefix.startswith("https://"):
        head = start + 8
    elif prefix.startswith("http://"):
        head = start + 7
    elif prefix.startswith("www."):
        head = start + 4
    else:
        return start

    # 记录配对的右括号最远位置，剥除尾部时可区分 URL 内的括号与外部括号。
    parens = brackets = 0
    paired_paren = query = fragment = -1
    paired_brackets: list[int] = []
    end = head
    while end < len(chars) and _URL_CHAR.fullmatch(chars[end]):
        char = chars[end]
        if char == "(":
            parens += 1
        elif char == ")" and parens:
            parens -= 1
            paired_paren = end
        elif char == "[":
            brackets += 1
        elif char == "]" and brackets:
            brackets -= 1
            paired_brackets.append(end)
        elif char == "?" and query < 0 and fragment < 0:
            query = end
        elif char == "#" and fragment < 0:
            fragment = end
        end += 1

    while end > head:
        tail = end - 1
        char = chars[tail]
        if char in _URL_TAIL or (char == ")" and tail > paired_paren):
            end -= 1
        elif char == "]":
            if not paired_brackets or tail > paired_brackets[-1]:
                end -= 1
                continue
            opening = tail - 1
            while opening > head and tail - opening < _CITATION_MAX and chars[opening] != "[":
                opening -= 1
            if (
                opening <= head
                or (0 <= query < opening and (fragment < 0 or opening < fragment))
                or chars[opening] != "["
                or _citation_end(chars, opening) != end
            ):
                break
            end = opening
            # 已剥除配对的引文括号，后续尾括号须按仍在 URL 中的配对来判定。
            paired_brackets.pop()
        else:
            break
    return end if end > head else start


def _number_end(chars: list[str], start: int) -> tuple[int, bool]:
    """单趟消费数字串，即使没有小数点也无需从每个数字重新查找。"""
    end = start + int(chars[start] in ("v", "V"))
    decimal = False
    while True:
        while end < len(chars) and chars[end] in _ASCII_DIGITS:
            end += 1
        if end + 1 >= len(chars) or chars[end] != "." or chars[end + 1] not in _ASCII_DIGITS:
            return end, decimal
        decimal = True
        end += 1


def _split_atoms(text: str) -> tuple[list[_Atom], list[int]]:
    units = [unit for unit in RICH_ATOM.finditer(text) if not RICH_TAG.fullmatch(unit[0])]
    chars = [unit[0] for unit in units]
    atoms: list[_Atom] = []
    prefix = [0]
    start = 0
    while start < len(units):
        char = chars[start]
        end = start
        if char in ("h", "H", "w", "W"):
            end = _url_end(chars, start)
        elif char in ("[", "［"):
            end = _citation_end(chars, start)
        elif char in _ASCII_DIGITS or (
            char in ("v", "V") and start + 1 < len(chars) and chars[start + 1] in _ASCII_DIGITS
        ):
            end, decimal = _number_end(chars, start)
            if not decimal:
                for i in range(start, end):
                    atoms.append(_Atom(units[i].start(), units[i].end(), chars[i]))
                    prefix.append(prefix[-1] + 1)
                start = end
                continue
        end = max(start + 1, end)
        atoms.append(_Atom(units[start].start(), units[end - 1].end(), char if end == start + 1 else ""))
        prefix.append(prefix[-1] + end - start)
        start = end
    return atoms, prefix


def _is_ascii_word_atom(atom: _Atom) -> bool:
    return len(atom.char) == 1 and (atom.char.isascii() and (atom.char.isalnum() or atom.char == "_"))


def _avoid_mid_word(atoms: list[_Atom], start: int, target: int, limit: int) -> int:
    """回退切点若落在短 ASCII 英文词/标识符中间（如 fun|ction），贴齐到较近的合法词界，不破坏长纯数字串。"""
    if not (start < target < len(atoms)):
        return target
    if not (_is_ascii_word_atom(atoms[target - 1]) and _is_ascii_word_atom(atoms[target])):
        return target
    left = target - 1
    while left > start and _is_ascii_word_atom(atoms[left - 1]) and target - left < 32:
        left -= 1
    right = target + 1
    while right < len(atoms) and _is_ascii_word_atom(atoms[right]) and right - left <= 32:
        right += 1
    if right - left > 32 or not any(atoms[k].char.isalpha() for k in range(left, right)):
        return target
    candidates = [pos for pos in (left, right) if start < pos <= limit]
    if not candidates:
        return target
    return min(candidates, key=lambda pos: (abs(pos - target), pos))


def _cut_near(atoms: list[_Atom], start: int, target: int, limit: int, radius: int = _SEARCH) -> int:
    for punctuation in (_STRONG_BREAK_AFTER, _BREAK_AFTER):
        for d in range(radius + 1):
            for pos in (target - d, target + d):
                if start < pos <= limit and atoms[pos - 1].char in punctuation:
                    return pos
    return _avoid_mid_word(atoms, start, target, limit)


def split_by_parts(text: str, part_chars: list[int]) -> list[str]:
    """把跨栏/跨页段落的译文按各块原文长度拆开，优先在标点处断开（与前端 shared/text/split.ts 一致）。"""
    if len(part_chars) <= 1:
        return [text]
    atoms, prefix = _split_atoms(text)
    total = sum(part_chars) or 1
    visible = prefix[-1]
    targets: list[int] = []
    cumulative = 0
    for chars in part_chars[:-1]:
        cumulative += chars
        targets.append((2 * visible * cumulative + total) // (2 * total))
    pieces: list[str] = []
    start = offset = nearest = 0
    for i, target in enumerate(targets):
        if start >= len(atoms):
            pieces.append(text[offset:])
            offset = len(text)
            continue
        # 目标与合法索引区间单调前移。同距保留左边界，给剩余块预留位置。
        limit = max(start + 1, len(atoms) - (len(part_chars) - i - 1))
        nearest = max(nearest, start + 1)
        while nearest < limit and prefix[nearest] + prefix[nearest + 1] < 2 * target:
            nearest += 1
        cur_span = nearest - start
        if i + 1 < len(targets):
            next_limit = min(len(atoms), max(nearest + 1, len(atoms) - (len(part_chars) - i - 2)))
            next_nearest = min(next_limit, nearest + 1)
            while next_nearest < next_limit and prefix[next_nearest] + prefix[next_nearest + 1] < 2 * targets[i + 1]:
                next_nearest += 1
            next_span = max(0, next_nearest - nearest)
        else:
            next_span = len(atoms) - nearest
        radius = min(_SEARCH, max(4, min(cur_span, next_span) // 2))
        cut = _cut_near(atoms, start, nearest, limit, radius)
        end = atoms[cut - 1].end
        while True:
            if closing := _CLOSING_TAGS.match(text, end):
                end = closing.end()
            if cut == len(atoms) or atoms[cut].start != end or atoms[cut].char not in _NO_START:
                break
            end = atoms[cut].end
            cut += 1
        pieces.append(text[offset:end])
        offset = end
        start = cut
    pieces.append(text[offset:])
    return _balance_rich_parts(pieces)


def _balance_rich_parts(parts: list[str]) -> list[str]:
    """跨块样式在每块末尾关闭、下块开头重开；未知标签继续作为普通文字。"""
    active: list[str] = []
    out: list[str] = []
    for part in parts:
        visible = bool(RICH_TAG.sub("", part))
        prefix = "".join(f"<{tag}>" for tag in active) if visible else ""
        for match in re.finditer(r"<(/?)(sup|sub|b|i)>", part):
            close, tag = match.groups()
            if not close:
                active.append(tag)
            elif active and active[-1] == tag:
                active.pop()
        if not visible:
            out.append("")
        else:
            out.append(prefix + part + "".join(f"</{tag}>" for tag in reversed(active)))
    return out


def role(block: Block, *, translated: bool = True) -> str:
    """排版角色，决定字体（与前端一致）。

    整块粗体是原文的字重信息，不代表译文是标题；只有未翻译的回退文本才沿用它，
    这样正文译文不会被整块切换成黑体粗体，局部 ``<b>`` 仍由排版器单独处理。
    """
    if block.type is BlockType.FIGURE_TEXT:
        return "label"
    if block.type is BlockType.TITLE or (not translated and block.style.bold):
        return "heading"
    if block.type is BlockType.CAPTION:
        return "caption"
    return "body"


_COLUMN_MIN_GAP = 8.0  # 双栏译文排版框与同页右栏块之间保留的最小安全栏距（pt）
_COLUMN_RIGHT_OFFSET = 100.0
_COLUMN_TOUCH_TOL = 6.0


def _box(block: Block) -> pymupdf.Rect:
    """排版框：原文块的范围。解析器给单行文字的框只有字形高，装不下一整行时上下各补一半，
    不再统一往下放宽（那样会压到紧挨着的表格线、图片，如表格标题）。"""
    r = block.bbox
    lack = block.style.font_size * LINE_HEIGHT + 0.5 - r.height
    pad = max(lack, 0) / 2
    return pymupdf.Rect(r.x0, r.y0 - pad, r.x1, r.y1 + pad)


def _clamp_column_right(block: Block, box: pymupdf.Rect, neighbors: Sequence[Block]) -> pymupdf.Rect:
    """双栏左栏块若与同页垂直重叠的右栏块栏距不足，则钳制左栏译文排版框右缘以保持 >= 8pt 栏距。"""
    if block.type is BlockType.FIGURE_TEXT or box.is_empty:
        return box
    limit = box.x1
    for other in neighbors:
        if other.id == block.id or other.type is BlockType.FIGURE_TEXT:
            continue
        ob = other.bbox
        if (
            ob.width > 0
            and ob.height > 0
            and ob.x0 > block.bbox.x0 + _COLUMN_RIGHT_OFFSET
            and ob.x0 >= block.bbox.x1 - _COLUMN_TOUCH_TOL
            and ob.y0 < box.y1
            and ob.y1 > box.y0
        ):
            limit = min(limit, ob.x0 - _COLUMN_MIN_GAP)
    if limit < box.x1 and limit - box.x0 >= 0.7 * block.bbox.width:
        return pymupdf.Rect(box.x0, box.y0, limit, box.y1)
    return box


def _formula_images(original: pymupdf.Document, seg: Segment) -> dict[str, tuple[bytes, float, float]]:
    """本段落行内公式的裁剪图：占位符 → (PNG, 宽, 高)，宽高为原文中的尺寸（pt）。"""
    out: dict[str, tuple[bytes, float, float]] = {}
    for key, ref in seg.formula_refs.items():
        if not 0 <= ref.page < len(original):
            continue
        if image := crop_formula(original[ref.page], ref.bbox):
            out[key] = image
    return out


def _safe_bottom(
    page: pymupdf.Page, box: pymupdf.Rect, right: float, block: Block, neighbors: Sequence[Block]
) -> float:
    bottom = max(box.y1, page.rect.y1 - BOTTOM_MARGIN)
    for other in neighbors:
        if other.id == block.id:
            continue
        r = _box(other) if other.translatable else other.bbox
        if r.x0 < right and r.x1 > box.x0 and r.y1 > box.y1:
            bottom = min(bottom, max(box.y1, r.y0))
    return bottom


def _line_bottom(line: Line, base: float, size: float) -> float:
    """行距之外也考虑字体下伸、下标和公式图的实际边界。"""
    return max(
        (base + size * 0.15 if tok.kind == "image"
         else max((base - run.rise - run.font.descender * run.size for run in tok.runs), default=base)
         for tok in line.tokens),
        default=base,
    )  # fmt: skip


_SECTION_HEADING_PREFIX = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?|[IVXLCDM]+\.|[A-Z]\.)\s+\S")


def _heading_align(block: Block, page_width: float) -> str:
    """单栏居中主标题或对称居中大标题自动居中，栏内偏左小标题保持左对齐。"""
    if block.type is not BlockType.TITLE or page_width <= 0:
        return "left"
    x0, x1 = block.bbox.x0, block.bbox.x1
    left_margin, right_margin = x0, page_width - x1
    if abs((x0 + x1) / 2 - page_width / 2) > 0.05 * page_width:
        return "left"
    if left_margin >= 0.14 * page_width and right_margin >= 0.14 * page_width:
        return "center"
    if left_margin >= 0.06 * page_width and right_margin >= 0.06 * page_width:
        plain = RICH_TAG.sub("", " ".join(s.text for s in block.spans if s.kind == "text")).strip()
        if not _SECTION_HEADING_PREFIX.match(plain):
            return "center"
    return "left"


def _expand_title_box(block: Block, box: pymupdf.Rect, page_width: float, neighbors: Sequence[Block]) -> pymupdf.Rect:
    if block.type is not BlockType.TITLE or box.is_empty or page_width <= 0:
        return box
    if _heading_align(block, page_width) != "left":
        return box
    mid = page_width / 2
    left = block.bbox.x1 <= mid + block.style.font_size
    right = block.bbox.x0 >= mid - block.style.font_size
    max_left = block.bbox.x0 + max(24.0, 3 * block.style.font_size)
    bodies = [b for b in neighbors if b.type is BlockType.TEXT and b.page == block.page and b.id != block.id]
    has_left_col = any(b.bbox.x0 <= max_left and b.bbox.x1 <= mid + b.style.font_size for b in bodies)
    has_right_col = any(b.bbox.x0 >= mid - b.style.font_size for b in bodies)
    col_right = box.x1
    safe_right = page_width - BOTTOM_MARGIN
    for other in neighbors:
        if other.id == block.id or other.page != block.page:
            continue
        ob = other.bbox
        if ob.y0 < box.y1 and ob.y1 > box.y0 and ob.x0 >= block.bbox.x1 - 2:
            safe_right = min(safe_right, ob.x0 - _COLUMN_MIN_GAP)
    for other in bodies:
        ob = other.bbox
        if ob.x0 > max_left:
            continue
        if left and has_left_col:
            same_column = ob.x1 <= mid + other.style.font_size
        elif right and has_right_col:
            same_column = ob.x0 >= mid - other.style.font_size
        elif (left or right) and (has_left_col or has_right_col):
            continue  # 只有对栏正文时，也不能借用全幅摘要的右缘。
        else:
            same_column = ob.x1 >= block.bbox.x1 - 2
        if same_column:
            col_right = max(col_right, ob.x1)
    x1 = min(col_right, safe_right)
    return pymupdf.Rect(box.x0, box.y0, x1, box.y1) if x1 > box.x1 else box


def _place(
    ts: Typesetter,
    page: pymupdf.Page,
    block: Block,
    tokens: list[Token],
    indent: bool,
    images: dict[str, tuple[bytes, float, float]],
    placed: list[Placed] | None = None,
    translated: bool = True,
    *,
    neighbors: Sequence[Block] = (),
    placed_images: dict[str, list[pymupdf.Rect]] | None = None,
) -> bool:
    """先收紧行距再缩小字号。返回译文是否有行未写入 PDF。"""
    box = _expand_title_box(block, _box(block) & page.rect, page.rect.width, neighbors)
    box = _clamp_column_right(block, box, neighbors)
    if box.is_empty:
        return translated and any(
            token.kind == "image" or (token.kind != "space" and any(run.text for run in token.runs)) for token in tokens
        )
    overflow = False
    size = block.style.font_size
    r = role(block, translated=translated)
    align = _heading_align(block, page.rect.width) if r == "heading" else "center" if r == "label" else "justify"
    box_limit = box.height + 0.5

    def layout(scale: float, pitch: float) -> tuple[list[Line], list[float], float]:
        lines, bases, height = ts.layout(
            tokens,
            box.width / scale,
            size,
            pitch,
            indent,
            balance_heading=r == "heading" and block.type is BlockType.TITLE,
            balance_caption=r == "caption" and block.type is BlockType.CAPTION,
        )
        return lines, bases, height * scale

    def safe_height(lines: list[Line], scale: float) -> float:
        # 悬挂标点和不可断开的西文词可能超出排版框的右边缘。
        right = max(
            (box.x0 + (sum(line.glue) + sum(t.width for t in line.tokens)
                       + (2 * size if indent and i == 0 else 0)) * scale
             for i, line in enumerate(lines)),
            default=box.x1,
        )  # fmt: skip
        return _safe_bottom(page, box, max(box.x1, right), block, neighbors) - box.y0

    def fits(lines: list[Line], bases: list[float], height: float, scale: float, *, borrow: bool = False) -> bool:
        if height > box_limit and not borrow:
            return False
        bottoms = (_line_bottom(line, base, size) for line, base in zip(lines, bases, strict=True))
        drawn = max(bottoms, default=0) * scale
        # 框内按排版行高适配，字形下伸只需避开邻块与页底。借用空白还需容纳完整的排版行。
        extent = max(height, drawn) if height > box_limit else drawn
        return extent <= box.height or extent <= safe_height(lines, scale)

    scale = 1.0
    for pitch in LINE_HEIGHTS:
        lines, bases, height = layout(scale, pitch)
        if fits(lines, bases, height, scale):
            break
        if block.type is BlockType.TITLE and pitch == LINE_HEIGHT and len(lines) <= 2:
            drawn = max((_line_bottom(line, base, size) for line, base in zip(lines, bases, strict=True)), default=0)
            if max(height, drawn) <= safe_height(lines, scale) - 2:
                break
    else:
        lines, bases, height = layout(MIN_SCALE, LINE_HEIGHT)
        needs_gap = height > box_limit
        if fits(lines, bases, height, MIN_SCALE, borrow=needs_gap):
            lo, hi = MIN_SCALE, 1.0
            for _ in range(10):
                mid = (lo + hi) / 2
                candidate, baselines, height = layout(mid, LINE_HEIGHT)
                lo, hi = (mid, hi) if fits(candidate, baselines, height, mid, borrow=needs_gap) else (lo, mid)
            scale = lo
            lines, bases, _ = layout(scale, LINE_HEIGHT)
        else:
            scale = MIN_SCALE
            available = safe_height(lines, scale)
            bottoms = [_line_bottom(line, base, size) for line, base in zip(lines, bases, strict=True)]
            log.warning(
                "译文导出溢出 page=%d block_id=%s overflow=%.2fpt",
                block.page + 1,
                block.id,
                max(height, max(bottoms) * scale) - available,
            )
            count = next((i for i, bottom in enumerate(bottoms) if bottom * scale > available), len(lines))
            if count == 0:
                # 去字后至少保留首行。极矮框允许首行低于常规最小字号。
                scale, count = available / bottoms[0], 1
            overflow = translated and count < len(lines)
            lines, bases = lines[:count], bases[:count]
    if not lines:
        return False
    c = block.style.color
    writer = pymupdf.TextWriter(page.rect)
    ts.draw(page, writer, (box.x0, box.y0), box.width / scale, lines, bases, size, align, indent,
            lambda key: images[key][0] if key in images else None, scale, placed, placed_images)  # fmt: skip
    writer.write_text(page, color=((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255))
    return overflow


def _exported_text(seg: Segment) -> str:
    return seg.target or seg.source


@dataclass
class _RenderGroup:
    block: Block | None  # 首块的样式和合区坐标；缺块也保留权重，避免后面的译文错位。
    member_ids: set[str]
    chars: int


def _merged_bounds(prev: Block, nxt: Block, bounds: Rect, members: set[str], neighbors: Sequence[Block]) -> Rect | None:
    if prev.page != nxt.page or prev.type is not BlockType.TEXT or nxt.type is not BlockType.TEXT:
        return None
    a, b = prev.bbox, nxt.bbox
    size = max(prev.style.font_size, nxt.style.font_size)
    min_width = min(a.width, b.width)
    if (
        min_width <= 0
        or min(a.height, b.height) <= 0
        or min(a.x1, b.x1) - max(a.x0, b.x0) < 0.70 * min_width
        or abs(a.x0 - b.x0) > 1.2 * size
        or b.x1 > a.x1 + 1.5 * size
        or not -0.5 * size <= b.y0 - a.y1 <= 1.5 * size
    ):
        return None
    union = Rect(x0=min(bounds.x0, b.x0), y0=min(bounds.y0, b.y0), x1=max(bounds.x1, b.x1), y1=max(bounds.y1, b.y1))
    # 后续块可能把整个外接框加宽，早先块旁的图也必须避开。
    for other in neighbors:
        if other.id != nxt.id and other.id not in members and union.intersection(other.bbox) is not None:
            return None
    return union


def _merge_render_parts(seg: Segment, blocks: dict[str, Block], by_page: dict[int, list[Block]]) -> list[_RenderGroup]:
    parts = seg.parts or [SegmentPart(block_id=bid, chars=len(seg.source)) for bid in seg.block_ids[:1]]
    groups: list[_RenderGroup] = []
    prev = None
    for part in parts:
        block = blocks.get(part.block_id)
        group = groups[-1] if groups else None
        if prev is not None and block is not None and group is not None and group.block is not None:
            merged = _merged_bounds(prev, block, group.block.bbox, group.member_ids, by_page[block.page])
            if merged is not None:
                group.block = group.block.model_copy(update={"bbox": merged})
                group.member_ids.add(block.id)
                group.chars += part.chars
                prev = block
                continue
        groups.append(_RenderGroup(block, {part.block_id}, part.chars))
        prev = block
    return groups


def block_pieces(
    doc: Document, segments: list[Segment], shown: Callable[[Segment], str] = _exported_text
) -> dict[str, str]:
    """块 id → 这一块显示的文字（拆分方式与 _typeset_all、前端一致）。shown：一段显示译文还是原文。"""
    blocks = {b.id: b for b in doc.blocks()}
    by_page = {page.index: page.blocks for page in doc.pages}
    out: dict[str, str] = {}
    for seg in segments:
        groups = _merge_render_parts(seg, blocks, by_page)
        texts = split_by_parts(shown(seg), [group.chars for group in groups])
        for group, piece in zip(groups, texts, strict=False):
            if group.block is not None:
                out.update(dict.fromkeys(group.member_ids, ""))
                out[group.block.id] = piece
    return out


def _typeset_all(
    src: pymupdf.Document,
    out: pymupdf.Document,
    doc: Document,
    segments: list[Segment],
    font_dir: Path | None,
) -> tuple[dict[str, list[Placed]], dict[str, dict[str, list[pymupdf.Rect]]], list[int]]:
    """把译文和回退原文排进去字背景页，返回落位文字、落位公式图矩形和截断译文的原页码。"""
    placed: dict[str, list[Placed]] = {}
    placed_images: dict[str, dict[str, list[pymupdf.Rect]]] = {}
    overflow_pages: set[int] = set()
    ts = Typesetter(FontSet.load(font_dir))
    blocks = {b.id: b for b in doc.blocks()}
    by_page = {page.index: page.blocks for page in doc.pages}
    for seg in segments:
        images = _formula_images(src, seg)
        groups = _merge_render_parts(seg, blocks, by_page)
        texts = split_by_parts(_exported_text(seg), [group.chars for group in groups])
        for n, (group, piece) in enumerate(zip(groups, texts, strict=False)):
            block = group.block
            if block is None or not piece.strip() or block.page >= len(out):
                continue
            r = role(block, translated=bool(seg.target))
            caption_no = ""
            if r == "caption" and n == 0 and seg.target and (m := CAPTION_NO.match(piece)):
                caption_no = m.group(0)
            size = lambda key, imgs=images: imgs[key][1:] if key in imgs else None  # noqa: E731
            text = normalize_cjk_spacing(piece.strip()) if seg.target else piece.strip()
            tokens = ts.tokenize(drop_fake_scripts(text), r, block.style.font_size, size, caption_no,
                                 formulas=seg.formulas)  # fmt: skip
            indent = bool(seg.target) and wants_indent(block, seg, first=n == 0)
            neighbors = by_page[block.page]
            runs = placed.setdefault(block.id, [])
            drawn_images = placed_images.setdefault(block.id, {})
            if len(group.member_ids) > 1:
                neighbors = [other for other in neighbors if other.id not in group.member_ids]
                for bid in group.member_ids:
                    placed[bid] = runs
                    placed_images[bid] = drawn_images
            overflow = _place(
                ts, out[block.page], block, tokens, indent, images, runs, bool(seg.target),
                neighbors=neighbors, placed_images=drawn_images,
            )  # fmt: skip
            if overflow:
                overflow_pages.add(block.page + 1)
    return placed, placed_images, sorted(overflow_pages)


def _rects(runs: list[Placed], needle: str, nth: int) -> list[pymupdf.Rect]:
    """紧凑文字 needle 第 nth 处在页面上占的矩形（跨行时每行一个）。"""
    where = [(r, i) for r, run in enumerate(runs) for i, ch in enumerate(run.text) if not ch.isspace()]
    text = "".join(runs[r].text[i] for r, i in where)
    starts = [m.start() for m in re.finditer(re.escape(needle), text)] if needle else []
    if nth >= len(starts):
        return []
    span = where[starts[nth] : starts[nth] + len(needle)]
    lines: dict[float, pymupdf.Rect] = {}
    for r in dict.fromkeys(r for r, _ in span):
        idx = [i for rr, i in span if rr == r]
        rect = runs[r].span(idx[0], idx[-1] + 1)
        key = round(runs[r].baseline, 1)
        lines[key] = lines[key] | rect if key in lines else rect
    return list(lines.values())


def _insert(page: pymupdf.Page, link: PdfLink, rect: pymupdf.Rect, dx: float = 0.0) -> None:
    """dx：双语对照拼页时目标页上同一半的横向偏移，让跳转目标落在同一半。"""
    if link.uri:
        page.insert_link({"kind": pymupdf.LINK_URI, "from": rect, "uri": link.uri})
    elif link.dest_page is not None:
        # extract_links 统一令 dest_y == dest_top（MuPDF 左上原点）；保留 dest_y 回退以兼容手造测试夹具
        dest_top = link.dest_top or link.dest_y
        page.insert_link({"kind": pymupdf.LINK_GOTO, "from": rect, "page": link.dest_page,
                          "to": pymupdf.Point(link.dest_x + dx, dest_top)})  # fmt: skip


def _translated_rects(link: PdfLink, placed: dict[str, list[Placed]]) -> list[pymupdf.Rect]:
    """链接在译文页上的位置：译文块外的原位置不变；块内的按译文里找到的文字；找不到返回空。"""
    r = link.rect
    if link.block_id is None:
        return [pymupdf.Rect(r.x0, r.y0, r.x1, r.y1)]
    return _rects(placed.get(link.block_id, []), link.text, link.nth) if link.text else []


def _rect_intersection_area(a: Rect, b: Rect) -> float:
    return max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0)) * max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def _formula_link_rects(
    link: PdfLink,
    placed_images: dict[str, dict[str, list[pymupdf.Rect]]],
    block_to_seg: dict[str, Segment],
    block_pages: dict[str, int],
) -> tuple[int, list[pymupdf.Rect]] | None:
    """文本未定位到的块内链接若落在原文行内公式框内，按相对坐标映射到译文页绘出的公式图矩形。"""
    if link.block_id is None or link.text or link.kind in SKIP_DESTS:
        return None
    seg = block_to_seg.get(link.block_id)
    if seg is None or not seg.formula_refs:
        return None
    link_area = max(link.rect.width * link.rect.height, 1e-6)
    best: tuple[float, float, str, FormulaRef] | None = None
    for key, ref in seg.formula_refs.items():
        if ref.page != link.page or ref.bbox.width <= 0 or ref.bbox.height <= 0:
            continue
        expanded = Rect(x0=ref.bbox.x0 - 2.0, y0=ref.bbox.y0 - 1.5, x1=ref.bbox.x1 + 2.0, y1=ref.bbox.y1 + 1.5)
        ratio = _rect_intersection_area(link.rect, expanded) / link_area
        if ratio < 0.5:
            continue
        raw_inter = _rect_intersection_area(link.rect, ref.bbox)
        cand = (ratio, raw_inter, key, ref)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is None:
        return None
    _, _, key, ref = best
    target_bid: str | None = None
    drawn_rects: list[pymupdf.Rect] = []
    search_order = [link.block_id, *(bid for bid in seg.block_ids if bid != link.block_id)]
    for bid in search_order:
        if rects := placed_images.get(bid, {}).get(key):
            target_bid = bid
            drawn_rects = rects
            break
    if target_bid is None or not drawn_rects:
        return None
    fb = ref.bbox
    rx0 = _clamp((link.rect.x0 - fb.x0) / fb.width, 0.0, 1.0)
    rx1 = _clamp((link.rect.x1 - fb.x0) / fb.width, 0.0, 1.0)
    ry0 = _clamp((link.rect.y0 - fb.y0) / fb.height, 0.0, 1.0)
    ry1 = _clamp((link.rect.y1 - fb.y0) / fb.height, 0.0, 1.0)
    mapped: list[pymupdf.Rect] = []
    for dst in drawn_rects:
        if dst.width <= 0 or dst.height <= 0:
            continue
        mx0 = dst.x0 + rx0 * dst.width
        mx1 = dst.x0 + rx1 * dst.width
        my0 = dst.y0 + ry0 * dst.height
        my1 = dst.y0 + ry1 * dst.height
        mw = mx1 - mx0
        if mw < 4.0:
            target_w = min(dst.width, max(mw, 4.0))
            cx = (mx0 + mx1) / 2
            mx0 = _clamp(cx - target_w / 2, dst.x0, dst.x1 - target_w)
            mx1 = mx0 + target_w
        mh = my1 - my0
        if mh < 4.0:
            target_h = min(dst.height, max(mh, 0.45 * dst.height))
            cy = (my0 + my1) / 2
            my0 = _clamp(cy - target_h / 2, dst.y0, dst.y1 - target_h)
            my1 = my0 + target_h
        rect = pymupdf.Rect(mx0, my0, mx1, my1)
        if not rect.is_empty:
            mapped.append(rect)
    if not mapped:
        return None
    return block_pages.get(target_bid, link.page), mapped


def _resolved_translated_rects(
    link: PdfLink,
    placed: dict[str, list[Placed]],
    placed_images: dict[str, dict[str, list[pymupdf.Rect]]],
    block_to_seg: dict[str, Segment],
    block_pages: dict[str, int],
) -> tuple[int, list[pymupdf.Rect]]:
    rects = _translated_rects(link, placed)
    if rects:
        return link.translated_page(block_pages), rects
    fallback = _formula_link_rects(link, placed_images, block_to_seg, block_pages)
    if fallback is not None:
        return fallback
    return link.translated_page(block_pages), []


def page_links(original: Path, doc: Document, segments: list[Segment]) -> list[PdfLink]:
    """原文的全部链接（尚未在译文里定位）：译文块里的链接要在译文里重新定位。"""
    return extract_links(original, doc, {part.block_id for seg in segments for part in seg.parts})


def place_links(
    links: list[PdfLink], doc: Document, segments: list[Segment], shown: Callable[[Segment], str] = _exported_text
) -> list[PdfLink]:
    siblings = {bid: seg.block_ids for seg in segments for bid in seg.block_ids}
    return locate(links, block_pieces(doc, segments, shown), siblings)


def export_pdf(
    original: Path,
    background: Path,
    doc: Document,
    segments: list[Segment],
    mode: ExportMode,
    font_dir: Path | None = None,
    side: Side = "right",
    links: list[PdfLink] | None = None,
) -> bytes:
    """返回导出的 PDF 字节。font_dir 缺字体时用内置字体。links：原文链接（调用方已缓存时传入，省得重读）。"""
    data, _ = _export(original, background, doc, segments, mode, font_dir, side, links,
                      lambda pdf: pdf.tobytes(garbage=3, deflate=True))  # fmt: skip
    return data


def export_pdf_to_path(
    original: Path,
    background: Path,
    doc: Document,
    segments: list[Segment],
    mode: ExportMode,
    font_dir: Path | None = None,
    side: Side = "right",
    links: list[PdfLink] | None = None,
    *,
    out: Path,
) -> list[int]:
    """直接写盘并返回有译文截断的原 PDF 页码（从 1 起）。"""
    with pdf_destination(out) as tmp:
        _, overflow_pages = _export(original, background, doc, segments, mode, font_dir, side, links,
                                    lambda pdf: pdf.save(tmp, garbage=3, deflate=True))  # fmt: skip
    return overflow_pages


def _export[T](
    original: Path,
    background: Path,
    doc: Document,
    segments: list[Segment],
    mode: ExportMode,
    font_dir: Path | None,
    side: Side,
    links: list[PdfLink] | None,
    save: Callable[[pymupdf.Document], T],
) -> tuple[T, list[int]]:
    raw = links if links is not None else page_links(original, doc, segments)
    links = place_links(raw, doc, segments)
    block_pages = {b.id: b.page for b in doc.blocks()}
    block_to_seg = {bid: seg for seg in segments for bid in seg.block_ids}
    with pymupdf.open(original) as src, pymupdf.open(background) as translated:
        placed, placed_images, overflow_pages = _typeset_all(src, translated, doc, segments, font_dir)
        translated.subset_fonts()  # 中文字体只保留用到的字形
        if mode == "mono":
            # 去字时有些 PDF 里译文块内的旧链接没被删掉，停在原位置、下面已是别的译文：先删掉，再按译文位置补上
            stale = {(x.page, round(x.rect.x0), round(x.rect.y0)) for x in raw if x.block_id is not None}
            for page in translated:
                for old in page.get_links():
                    if (page.number, round(old["from"].x0), round(old["from"].y0)) in stale:
                        page.delete_link(old)
            for link in links:  # 译文块外的链接在背景页里本来就在，只补块内的
                if link.block_id is not None:
                    tr_page, rects = _resolved_translated_rects(link, placed, placed_images, block_to_seg, block_pages)
                    for rect in rects:
                        _insert(translated[tr_page], link, rect)
            return save(translated), overflow_pages

        with pymupdf.open() as out:
            for i in range(len(src)):
                w, h = src[i].rect.width, src[i].rect.height
                page = out.new_page(width=w * 2, height=h)
                left, right = (translated, src) if side == "left" else (src, translated)
                page.show_pdf_page(pymupdf.Rect(0, 0, w, h), left, i)
                page.show_pdf_page(pymupdf.Rect(w, 0, w * 2, h), right, i)

            # 拼页不带链接：两半各自加回，跳转目标落在同一半。各页宽度可能不同（横版页），偏移按各自那一页算
            def shifts(page_no: int) -> tuple[float, float]:
                """（原文这一半的横向偏移, 译文这一半的横向偏移）"""
                width = src[page_no].rect.width
                return (width, 0.0) if side == "left" else (0.0, width)

            for link in links:
                page, r = out[link.page], link.rect
                dest = shifts(link.dest_page or 0)
                dx_src, _ = shifts(link.page)
                _insert(page, link, pymupdf.Rect(r.x0 + dx_src, r.y0, r.x1 + dx_src, r.y1), dest[0])
                tr_page, rects = _resolved_translated_rects(link, placed, placed_images, block_to_seg, block_pages)
                _, dx_tr = shifts(tr_page)
                for rect in rects:
                    _insert(
                        out[tr_page], link, pymupdf.Rect(rect.x0 + dx_tr, rect.y0, rect.x1 + dx_tr, rect.y1), dest[1]
                    )
            return save(out), overflow_pages
