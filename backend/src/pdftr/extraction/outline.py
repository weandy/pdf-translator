"""PDF 自带的书签（目录）。作者生成的书签最可靠，所以目录以它为准；没有书签时由调用方用识别出的标题代替。"""

from pathlib import Path

import pymupdf

from pdftr.domain.ir import OutlineItem


def pdf_outline(pdf: Path) -> list[OutlineItem]:
    out: list[OutlineItem] = []
    with pymupdf.open(pdf) as doc:
        for level, title, page_no, dest in doc.get_toc(simple=False):
            if not title.strip() or not 1 <= page_no <= len(doc):
                continue
            page = doc[page_no - 1]
            to = dest.get("to") if isinstance(dest, dict) else None
            top = max(page.rect.height - to.y, 0.0) if to is not None else 0.0  # 书签目标的 y 自下而上
            out.append(OutlineItem(title=title.strip(), level=max(int(level), 1), page=page_no - 1, top=top))
    return out
