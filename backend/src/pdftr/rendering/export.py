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

from pdftr.domain.ir import Block, BlockType, Document, Segment
from pdftr.domain.text import RICH_ATOM, RICH_TAG
from pdftr.rendering.artifacts import pdf_destination
from pdftr.rendering.background import crop_formula
from pdftr.rendering.links import PdfLink, extract_links, locate
from pdftr.rendering.typeset import LINE_HEIGHTS, drop_fake_scripts, normalize_cjk_spacing, wants_indent
from pdftr.rendering.typesetter import _NO_START, FontSet, Line, Placed, Token, Typesetter

log = logging.getLogger(__name__)

ExportMode = Literal["mono", "dual"]
Side = Literal["left", "right"]  # 双语对照时译文放在哪一边
LINE_HEIGHT = 1.2  # 最紧的行距（单行框补高按它计算）
MIN_SCALE = 0.4  # 常规适配的最小字号比例，极矮框的首行可继续缩小
BOTTOM_MARGIN = 4.0

_BREAK_AFTER = set("，。；：、）！？,;:.)")
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
    for d in range(radius + 1):
        for pos in (target - d, target + d):
            if start < pos <= limit and atoms[pos - 1].char in _BREAK_AFTER:
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


def _box(block: Block) -> pymupdf.Rect:
    """排版框：原文块的范围。解析器给单行文字的框只有字形高，装不下一整行时上下各补一半，
    不再统一往下放宽（那样会压到紧挨着的表格线、图片，如表格标题）。"""
    r = block.bbox
    lack = block.style.font_size * LINE_HEIGHT + 0.5 - r.height
    pad = max(lack, 0) / 2
    return pymupdf.Rect(r.x0, r.y0 - pad, r.x1, r.y1 + pad)


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
) -> bool:
    """先收紧行距再缩小字号。返回译文是否有行未写入 PDF。"""
    box = _box(block) & page.rect
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
            lambda key: images[key][0] if key in images else None, scale, placed)  # fmt: skip
    writer.write_text(page, color=((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255))
    return overflow


def _exported_text(seg: Segment) -> str:
    return seg.target or seg.source


def block_pieces(
    doc: Document, segments: list[Segment], shown: Callable[[Segment], str] = _exported_text
) -> dict[str, str]:
    """块 id → 这一块显示的文字（拆分方式与 _typeset_all、前端一致）。shown：一段显示译文还是原文。"""
    ids = {b.id for b in doc.blocks()}
    out: dict[str, str] = {}
    for seg in segments:
        texts = split_by_parts(shown(seg), [p.chars for p in seg.parts] or [len(seg.source)])
        for part, piece in zip(seg.parts, texts, strict=False):
            if part.block_id in ids:
                out[part.block_id] = piece
    return out


def _typeset_all(src: pymupdf.Document, out: pymupdf.Document, doc: Document, segments: list[Segment],
                 font_dir: Path | None) -> tuple[dict[str, list[Placed]], list[int]]:  # fmt: skip
    """把译文和回退原文排进去字背景页，返回落位文字和截断译文的原页码。"""
    placed: dict[str, list[Placed]] = {}
    overflow_pages: set[int] = set()
    ts = Typesetter(FontSet.load(font_dir))
    blocks = {b.id: b for b in doc.blocks()}
    by_page = {page.index: page.blocks for page in doc.pages}
    for seg in segments:
        images = _formula_images(src, seg)
        texts = split_by_parts(_exported_text(seg), [p.chars for p in seg.parts] or [len(seg.source)])
        for n, (part, piece) in enumerate(zip(seg.parts, texts, strict=False)):
            block = blocks.get(part.block_id)
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
            overflow = _place(
                ts, out[block.page], block, tokens, indent, images, placed.setdefault(block.id, []), bool(seg.target),
                neighbors=by_page[block.page],
            )  # fmt: skip
            if overflow:
                overflow_pages.add(block.page + 1)
    return placed, sorted(overflow_pages)


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
        page.insert_link({"kind": pymupdf.LINK_GOTO, "from": rect, "page": link.dest_page,
                          "to": pymupdf.Point(link.dest_x + dx, link.dest_y)})  # fmt: skip


def _translated_rects(link: PdfLink, placed: dict[str, list[Placed]]) -> list[pymupdf.Rect]:
    """链接在译文页上的位置：译文块外的原位置不变；块内的按译文里找到的文字；找不到返回空。"""
    r = link.rect
    if link.block_id is None:
        return [pymupdf.Rect(r.x0, r.y0, r.x1, r.y1)]
    return _rects(placed.get(link.block_id, []), link.text, link.nth) if link.text else []


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
    with pymupdf.open(original) as src, pymupdf.open(background) as translated:
        placed, overflow_pages = _typeset_all(src, translated, doc, segments, font_dir)
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
                    for rect in _translated_rects(link, placed):
                        _insert(translated[link.translated_page(block_pages)], link, rect)
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
                tr_page = link.translated_page(block_pages)
                _, dx_tr = shifts(tr_page)
                for rect in _translated_rects(link, placed):
                    _insert(
                        out[tr_page], link, pymupdf.Rect(rect.x0 + dx_tr, rect.y0, rect.x1 + dx_tr, rect.y1), dest[1]
                    )
            return save(out), overflow_pages
