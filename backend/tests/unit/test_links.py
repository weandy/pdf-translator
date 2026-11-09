"""链接文字在译文里的定位：引用编号、作者-年份、断词、图表编号、跨块、网址。"""

from collections import Counter
from pathlib import Path

import pymupdf
import pytest

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, Page
from pdftr.rendering.links import PdfLink, _link_anchor, _text_lines, compact, extract_links, locate


def _link(anchor: str, kind: str = "cite", block: str = "b1", y: float = 0, x: float = 0, uri: str = "") -> PdfLink:
    return PdfLink(page=0, rect=Rect(x0=x, y0=y, x1=x + 5, y1=y + 5), anchor=anchor, kind=kind, block_id=block,
                   uri=uri, dest_page=None if uri else 3)  # fmt: skip


def _found(links: list[PdfLink]) -> list[tuple[str, int]]:
    return [(x.text, x.nth) for x in links]


def test_citation_numbers_only_inside_brackets():
    # 正文里的 22 层不能配给引用 [22]；“[22,”“21]”带着标点
    pieces = {"b1": "我们用 22 层网络，见文献 [22, 21]。"}
    got = locate([_link("[22,", x=0), _link("21]", x=10)], pieces)
    text = compact(pieces["b1"])
    assert (
        _found(got) == [("22", 1), ("21", 0)]
        and text.find("[22") + 1 == [i for i in range(len(text)) if text.startswith("22", i)][1]
    )


def test_author_year_split_links_and_hyphenation():
    pieces = {"b1": "与以往工作（Peters et al., 2018a; Radford et al., 2018）不同"}
    got = locate([_link("Peters et al.", x=0), _link("2018a;", x=10), _link("Rad-", x=20), _link("ford et al.,", y=10),
                  _link("2018)", y=10, x=10)], pieces)  # fmt: skip
    assert _found(got) == [("Petersetal", 0), ("2018a", 0), ("Rad", 0), ("fordetal", 0), ("2018", 1)]


def test_figure_numbers_prefer_chinese_labels_and_skip_decimals():
    pieces = {"b1": "准确率 6.43%（见表 6 与图 2）"}
    got = locate([_link("6)", kind="table"), _link("2", kind="figure", x=10)], pieces)
    text = compact(pieces["b1"])
    assert [text[text.index("表") + 1]] == ["6"] and _found(got) == [("6", 1), ("2", 0)]


def test_footnotes_skipped_and_missing_text_left_empty():
    got = locate([_link("s1,", kind="Hfootnote"), _link("[99]")], {"b1": "没有这条引用 [12]"})
    assert _found(got) == [("", 0), ("", 0)]


def test_falls_back_to_sibling_blocks_of_the_same_paragraph():
    # 跨栏段落拆块后，引用落进了上一块的译文
    pieces = {"b1": "……（Howard and Ruder,", "b2": "2018）。"}
    got = locate([_link("(Howard and Ruder,", block="b2")], pieces, {"b1": ["b1", "b2"], "b2": ["b1", "b2"]})
    assert got[0].block_id == "b1" and got[0].text == "HowardandRuder"


def test_urls_cut_by_line_breaks_match_the_full_address():
    link = _link("http://mscoco.org/dataset/#de", kind="", uri="http://mscoco.org/dataset/#detections-challenge2015")
    pieces = {"b1": "见 http://mscoco.org/dataset/#detections-challenge2015。"}
    assert locate([link], pieces)[0].text == "http://mscoco.org/dataset/#detections-challenge2015"
    footnote = _link("1http://image-net.org/", kind="", uri="http://image-net.org/")  # 原文字带着脚注编号
    assert locate([footnote], {"b1": "1http://image-net.org/ 与"})[0].text == "http://image-net.org/"
    trailing = _link("google-research/bert.", kind="", uri="https://github.com/google-research/bert")
    assert locate([trailing], {"b1": "代码见 https://github.com/google-research/bert。"})[0].text == trailing.uri


def test_publisher_citation_dests_mailto_and_whole_block_links():
    # 出版社排版：引用跳转名是 CIT0057 一类（extract_links 归为 cite），年份在圆括号里
    got = locate([_link("2026)", kind="cite")], {"b1": "（Yao et al. 2026）。在 2026 年"})
    assert _found(got) == [("2026", 0)]
    mail = _link("a@b.cn\nc of", kind="", uri="mailto:a@b.cn")
    assert locate([mail], {"b1": "作者：A a@b.cn 某大学"})[0].text == "a@b.cn"
    button = _link("View related articles", kind="", uri="https://x.org/r").model_copy(update={"whole": True})
    assert locate([button], {"b1": "查看相关文章"})[0].text == "查看相关文章"


def test_extract_links_reuses_textpage_only_on_pages_with_retained_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pdf = tmp_path / "synthetic-links.pdf"
    with pymupdf.open() as src:
        for _ in range(4):
            src.new_page(width=300, height=200)
        first = src[0]
        first.insert_text((20, 40), "Reference [1]")
        first.insert_text((20, 70), "Project website")
        first.insert_link(
            {
                "kind": pymupdf.LINK_GOTO,
                "from": first.search_for("Reference [1]")[0],
                "page": 1,
                "to": pymupdf.Point(0, 20),
            }
        )
        first.insert_link(
            {
                "kind": pymupdf.LINK_URI,
                "from": first.search_for("Project website")[0],
                "uri": "https://example.com/project",
            }
        )
        # 无链接页不应创建 TextPage。
        src[1].insert_text((20, 40), "No links")
        src[2].insert_text((20, 40), "Also no links")
        last = src[3]
        last.insert_text((20, 40), "Contact author")
        last_rect = last.search_for("Contact author")[0]
        last.insert_link({"kind": pymupdf.LINK_URI, "from": last_rect, "uri": "mailto:author@example.com"})
        src.save(pdf)

    def block(block_id: str, page: int, box: pymupdf.Rect) -> Block:
        return Block(
            id=block_id,
            page=page,
            type=BlockType.TEXT,
            bbox=Rect(x0=box.x0, y0=box.y0, x1=box.x1, y1=box.y1),
            order=0,
            style=BlockStyle(font_size=11),
            translatable=True,
        )

    doc = Document(
        job_id="synthetic",
        pages=[
            Page(index=0, width=300, height=200, blocks=[block("b0", 0, pymupdf.Rect(10, 20, 250, 90))]),
            Page(index=1, width=300, height=200),
            Page(index=2, width=300, height=200),
            Page(index=3, width=300, height=200, blocks=[block("b3", 3, last_rect)]),
        ],
    )
    # 原来逐链接解析的锚点作为基准，确保复用不改变文字提取语义。
    with pymupdf.open(pdf) as src:
        expected_anchors = [src[0].get_textbox(raw["from"]).strip() for raw in src[0].get_links()]
        expected_anchors += [src[3].get_textbox(raw["from"]).strip() for raw in src[3].get_links()]

    calls: Counter[int] = Counter()
    get_textpage = pymupdf.Page.get_textpage

    def counted(page, *args, **kwargs):
        calls[page.number] += 1
        return get_textpage(page, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_textpage", counted)
    links = extract_links(pdf, doc, ["b0", "b3"])
    assert calls == {0: 1, 3: 1}
    assert [link.anchor for link in links] == expected_anchors == ["Reference [1]", "Project website", "Contact author"]
    assert [link.block_id for link in links] == ["b0", "b0", "b3"]
    assert [link.page for link in links] == [0, 0, 3]
    assert links[0].dest_page == 1 and links[0].uri == ""
    assert links[1].uri == "https://example.com/project" and links[1].dest_page is None
    assert links[2].uri == "mailto:author@example.com" and links[2].whole


def test_unsupported_link_actions_do_not_parse_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "synthetic-actions.pdf"
    with pymupdf.open() as src:
        src.new_page()
        src.save(pdf)
    # MuPDF 对不支持动作的返回结构；不能误把它作为网页或页内跳转保留。
    monkeypatch.setattr(
        pymupdf.Page, "get_links", lambda _: [{"kind": pymupdf.LINK_LAUNCH, "from": pymupdf.Rect(0, 0, 20, 20)}]
    )

    def forbidden(*args, **kwargs):
        pytest.fail("不支持的链接不应触发页面文字解析")

    monkeypatch.setattr(pymupdf.Page, "get_textpage", forbidden)
    assert extract_links(pdf, Document(job_id="synthetic", pages=[]), []) == []


def test_indexed_link_anchors_match_textbox_for_character_edges_lines_rotation_and_unicode():
    with pymupdf.open() as src:
        page = src.new_page(width=300, height=220)
        page.insert_text((20, 40), "Alpha beta gamma", fontsize=12)
        page.insert_text((20, 70), "Second line", fontsize=12)
        page.insert_text((220, 180), "Rotated label", fontsize=12, rotate=90)
        page.insert_text((20, 110), "示例链接", fontsize=12, fontname="china-s")
        textpage = page.get_textpage()
        lines = _text_lines(textpage)
        first_box = lines[0][1][0][0]
        rects = [
            page.search_for("Alpha beta")[0],
            # 框边横穿字形时仍取整字；仅贴边不应把左边的字选进来。
            pymupdf.Rect(first_box[2] - 0.25, first_box[1], first_box[2] + 0.25, first_box[3]),
            pymupdf.Rect(first_box[2], first_box[1], first_box[2] + 1, first_box[3]),
            pymupdf.Rect(10, 10, 200, 75),
            page.search_for("Rotated label")[0],
            page.search_for("示例链接")[0],
            pymupdf.Rect(280, 190, 290, 200),
        ]
        for rect in rects:
            assert _link_anchor(lines, rect) == page.get_textbox(rect, textpage=textpage).strip()
        assert _link_anchor(lines, rects[3]) == "Alpha beta gamma\nSecond line"
        assert _link_anchor(lines, rects[5]) == "示例链接"
