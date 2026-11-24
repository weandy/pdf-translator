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
            if to is None:
                top = 0.0
            elif dest.get("kind") == pymupdf.LINK_NAMED:
                # resolve_names() 返回 PDF 左下原点坐标，经目标页矩阵转为 MuPDF 左上原点坐标
                top = max((to * page.transformation_matrix).y, 0.0)
            else:
                # LINK_GOTO 的 to 已是 MuPDF 左上原点坐标
                top = max(to.y, 0.0)
            out.append(OutlineItem(title=title.strip(), level=max(int(level), 1), page=page_no - 1, top=top))
    return out
