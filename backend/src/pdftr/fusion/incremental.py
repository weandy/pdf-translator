"""只重建新增页，冻结页的文字和几何是已发布段落的永久坐标。"""

import unicodedata
from collections.abc import Sequence

from pdftr.domain.chars import CharPage
from pdftr.domain.ir import BlockType, Document
from pdftr.domain.layout import LayoutLine, LayoutPage, LayoutResult
from pdftr.domain.text import RICH_TAG
from pdftr.fusion.build import _fuse_relocated, _plain, _validate_layout
from pdftr.fusion.relocate import _line_text_key, relocate
from pdftr.fusion.structure import _bounds


def _key(text: str) -> str:
    plain = RICH_TAG.sub("", text)
    return "".join(c for c in unicodedata.normalize("NFKC", plain).lower() if c.isalnum())


def _strip_covered_lines(page: LayoutPage, chars: CharPage, tail: str) -> CharPage:
    first = min(
        (block for block in page.blocks if block.type is BlockType.TEXT and not block.discarded),
        key=lambda block: block.order,
        default=None,
    )
    if first is None or first.bbox.y0 > chars.height * 0.35:
        return chars
    prefix = ""
    count = 0
    for idx, line in enumerate(first.lines, 1):
        word = _line_text_key(line)
        if not word:
            continue
        prefix += word
        if len(prefix) >= 12 and tail.endswith(prefix):
            count = idx
    if not count:
        return chars

    covered = first.lines[:count]
    first.lines = first.lines[count:]
    if not first.lines:
        first.discarded = True
    else:
        first.bbox = _bounds([line.bbox for line in first.lines])
    return _strip_chars(chars, covered)


def _strip_chars(chars: CharPage, covered: Sequence[LayoutLine]) -> CharPage:
    # 防止已翻译的续行作为原生文字或孤立块复活。
    return chars.model_copy(
        update={
            "chars": [
                char for char in chars.chars if not any(line.bbox.contains_point(*char.bbox.center) for line in covered)
            ]
        }
    )


def fuse_incremental(
    old_doc: Document,
    merged_layout: LayoutResult,
    char_pages: Sequence[CharPage],
    frozen_pages: set[int],
    delta_pages: set[int],
) -> Document:
    _validate_layout(merged_layout, char_pages)
    old_pages = {page.index: page for page in old_doc.pages}
    coverage = {
        index: [_key(_plain(block)) for block in page.blocks if block.translatable]
        for index, page in old_pages.items()
        if index in frozen_pages
    }
    covered_moves: dict[int, list[LayoutLine]] = {}

    def can_move(source: int, target: int, line: LayoutLine) -> bool:
        if source in delta_pages and target in frozen_pages:
            # 冻结页没有收录续行时，将其留在新增页。
            key = _line_text_key(line)
            return bool(key) and any(key in text for text in coverage.get(target, []))
        if source in frozen_pages and target in delta_pages:
            key = _line_text_key(line)
            if key and any(key in text for text in coverage.get(source, [])):
                covered_moves.setdefault(target, []).append(line)
                return False
        return True

    relocated, report = relocate(merged_layout, char_pages=char_pages, can_move=can_move)
    new_layout = {page.index: page for page in relocated.pages if page.index in delta_pages}
    new_chars: list[CharPage] = []
    for char_page in char_pages:
        index = char_page.index
        if index not in delta_pages:
            continue
        if index in new_layout and index - 1 in frozen_pages and index - 1 in old_pages:
            previous = old_pages[index - 1]
            body = [b for b in previous.blocks if b.type is BlockType.TEXT and b.translatable]
            if body:
                tail = _key(_plain(max(body, key=lambda block: block.order)))
                char_page = _strip_covered_lines(new_layout[index], char_page, tail)
        if index in covered_moves:
            char_page = _strip_chars(char_page, covered_moves[index])
        new_chars.append(char_page)

    report.links = {
        target: source
        for target, source in report.links.items()
        if target[0] in delta_pages and source[0] in delta_pages
    }
    layout = relocated.model_copy(update={"pages": list(new_layout.values())})
    new_doc, _ = _fuse_relocated(
        old_doc.job_id,
        layout,
        new_chars,
        report,
        used_cloud=old_doc.meta.used_cloud,
        selected=delta_pages,
        expected_layout=delta_pages,
        restrict_meta=True,
    )
    frozen_ids = {b.id for page in old_doc.pages if page.index in frozen_pages for b in page.blocks}
    pages = old_pages | {page.index: page for page in new_doc.pages}
    for index, page in pages.items():
        if index not in frozen_pages and any(b.continues_from in frozen_ids for b in page.blocks):
            # 未补翻的旧页也可能含指向冻结页的链，复制后再断开。
            pages[index] = page.model_copy(
                update={
                    "blocks": [
                        b.model_copy(update={"continues_from": None}) if b.continues_from in frozen_ids else b
                        for b in page.blocks
                    ],
                }
            )
    meta = old_doc.meta
    if 0 in delta_pages:
        meta = meta.model_copy(
            update={
                "title": meta.title or new_doc.meta.title,
                "abstract": meta.abstract or new_doc.meta.abstract,
            }
        )
    return old_doc.model_copy(update={"pages": [pages[index] for index in sorted(pages)], "meta": meta})
