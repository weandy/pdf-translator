"""论文标题选择与跨平台下载文件名，全部使用合成文本。"""

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.document_name import MAX_DOWNLOAD_NAME_BYTES, download_filename, paper_title
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, DocMeta, Document, Page, Span
from pdftr.domain.job import Job, JobStatus
from pdftr.fusion.build import _meta
from pdftr.pipeline import usecases
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore


def block(text: str, order: int, *, y: float = 80, size: float = 20, kind: BlockType = BlockType.TITLE) -> Block:
    box = Rect(x0=50, y0=y, x1=500, y1=y + size)
    return Block(
        id=f"b{order}",
        page=0,
        type=kind,
        bbox=box,
        order=order,
        spans=[Span(text=text, bbox=box, size=size)],
        style=BlockStyle(font_size=size),
        translatable=True,
    )


def page(blocks: list[Block], index: int = 0) -> Page:
    return Page(index=index, width=600, height=800, blocks=blocks)


def test_title_skips_publication_header_and_sections_and_joins_adjacent_lines():
    p = page(
        [
            block("Journal of Synthetic Examples", 0, y=10, size=26),
            block("A Synthetic Approach", 1, y=80),
            block("to Reliable Scheduling", 2, y=105),
            block("Alice Example, Bob Example", 3, y=140, size=10, kind=BlockType.TEXT),
            block("Abstract", 4, y=180, size=14),
            block("A much larger section heading", 5, y=450, size=28),
        ]
    )
    assert paper_title([p]) == "A Synthetic Approach to Reliable Scheduling"
    assert _meta([p], "0123456789abcdef", True).title == "A Synthetic Approach to Reliable Scheduling"


def test_authors_do_not_join_title_even_when_their_font_matches():
    p = page([block("A Synthetic Title", 0), block("Alice Example", 1, y=105, kind=BlockType.TEXT)])
    assert paper_title([p]) == "A Synthetic Title"


@pytest.mark.parametrize("header", ["European Journal of Synthetic Studies", "Nature", "ScienceDirect"])
def test_larger_top_masthead_does_not_replace_paper_title(header):
    p = page([block(header, 0, y=10, size=26), block("A Synthetic Main Title", 1, y=80, size=20)])
    assert paper_title([p]) == "A Synthetic Main Title"


@pytest.mark.parametrize(
    "title", ["Introduction to Synthetic Systems", "Abstract Interpretation for Synthetic Systems"]
)
def test_paper_titles_beginning_with_section_words_are_not_section_labels(title):
    assert paper_title([page([block(title, 0), block("Abstract", 1, y=180, size=12)])]) == title


def test_split_main_title_does_not_appear_as_two_outline_sections(tmp_path):
    import pymupdf

    from pdftr.pipeline.usecases import build_outline

    p = page(
        [
            block("A Synthetic Approach", 0),
            block("to Reliable Scheduling", 1, y=105),
            block("Abstract", 2, y=200, size=12),
        ]
    )
    doc = Document(job_id="0123456789abcdef", pages=[p], meta=_meta([p], "synthetic", True))
    pdf = tmp_path / "synthetic.pdf"
    with pymupdf.open() as source:
        source.new_page()
        source.save(pdf)
    assert [item.title for item in build_outline(pdf, doc, [])] == ["Abstract"]


def test_title_can_follow_a_cover_but_does_not_search_arbitrary_later_pages():
    cover = page([block("Accepted Manuscript", 0)])
    actual = page([block("A Synthetic Main Title", 0)], 1)
    assert paper_title([cover, actual]) == "A Synthetic Main Title"
    assert paper_title([cover, page([], 1), page([block("An Arbitrary Later Heading", 0)], 2)]) == ""


def test_no_title_after_abstract_or_numbered_sections():
    p = page([block("Abstract", 0), block("2 Synthetic Method", 1, y=220)])
    assert paper_title([p, page([block("Unnumbered Later Heading", 0)], 1)]) == ""
    assert paper_title([p], fallback="Existing Synthetic Paper Title") == "Existing Synthetic Paper Title"
    assert paper_title([p], fallback="Introduction") == ""


def test_title_rejects_footer_and_unlabelled_body_text():
    p = page([block("An Unlabelled Body Paragraph", 0, kind=BlockType.TEXT), block("Some Footer Title", 1, y=760)])
    assert paper_title([p]) == ""


@pytest.mark.parametrize(
    ("title", "upload", "prefix"),
    [
        ("A Synthetic Title: A/B?", "id.pdf", "A_Synthetic_Title_A_B"),
        ("合成论文：模型 / 方法", "id.pdf", "合成论文_模型_方法"),
        ("<b>Strong</b> &amp; Safe", "id.pdf", "Strong_Safe"),
        ("A\u202e/B\x00 Title\r\nWith Tabs\t", "id.pdf", "A_B_Title_With_Tabs"),
        ("CON", "id.pdf", "_CON"),
        ("LPT1", "id.pdf", "_LPT1"),
        ("", r"C:\upload\fallback name.PDF", "fallback_name"),
        ("???", "../../fallback name.pdf", "fallback_name"),
        ("", "***.pdf", "document"),
        ("Full－Width：ＡＢＣ", "id.pdf", "Full_Width_ABC"),
    ],
)
def test_download_name_is_safe_and_preserves_title_or_fallback(title, upload, prefix):
    assert download_filename(title, upload, bilingual=False) == f"{prefix}_译文.pdf"
    assert download_filename(title, upload, bilingual=True) == f"{prefix}_双语对照.pdf"


def test_long_unicode_title_keeps_valid_utf8_and_both_download_suffixes():
    for bilingual in (False, True):
        name = download_filename("合成论文标题" * 100, "id.pdf", bilingual=bilingual)
        assert len(name.encode("utf-8")) <= MAX_DOWNLOAD_NAME_BYTES
        assert "�" not in name
        assert name.endswith("_双语对照.pdf" if bilingual else "_译文.pdf")


@pytest.fixture
def export_container(tmp_path, monkeypatch):
    container = build_container(Settings(data_dir=tmp_path, glossary_sources=""))
    pipeline = container.pipeline
    pipeline.deps.store.save_job(Job(id="0123456789abcdef", filename="random_upload_id.pdf", status=JobStatus.DONE))
    doc = Document(
        job_id="0123456789abcdef", pages=[page([block("A Synthetic Title", 0)])], meta=DocMeta(title="Old Title")
    )
    pipeline.deps.artifacts.path("0123456789abcdef", "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
    renders = []

    def render(*args):
        renders.append(True)
        return b"%PDF-synthetic-export"

    monkeypatch.setattr(usecases, "export_pdf", render)
    monkeypatch.setattr(usecases, "_raw_links", lambda *args: [])
    yield container, doc, renders
    assert isinstance(pipeline.deps.store, SqliteStore)
    assert isinstance(pipeline.deps.cache, SqliteTranslationCache)
    pipeline.deps.store.close()
    pipeline.deps.cache.close()


def test_export_uses_title_for_cached_and_new_files_without_changing_cache_path(export_container):
    container, doc, renders = export_container
    first, path = usecases.export_job(container.pipeline, "0123456789abcdef", "mono")
    assert first == "A_Synthetic_Title_译文.pdf"
    assert path.name.startswith("mono-right-") and path.read_bytes().startswith(b"%PDF")
    # 旧任务已经缓存PDF时仍会重新计算下载建议名，不需要重新生成文档。
    doc.pages[0].blocks[0].spans[0].text = "Updated Synthetic Title"
    container.pipeline.deps.artifacts.path("0123456789abcdef", "ir.json").write_text(
        doc.model_dump_json(), encoding="utf-8"
    )
    second, cached = usecases.export_job(container.pipeline, "0123456789abcdef", "mono")
    assert cached == path and second == "Updated_Synthetic_Title_译文.pdf"
    assert renders == [True]
    dual, _ = usecases.export_job(container.pipeline, "0123456789abcdef", "dual")
    assert dual == "Updated_Synthetic_Title_双语对照.pdf"


def test_export_falls_back_to_upload_name_when_ir_has_no_reliable_title(export_container):
    container, doc, _ = export_container
    doc.pages[0].blocks.clear()
    doc.meta.title = ""
    container.pipeline.deps.artifacts.path("0123456789abcdef", "ir.json").write_text(
        doc.model_dump_json(), encoding="utf-8"
    )
    filename, _ = usecases.export_job(container.pipeline, "0123456789abcdef", "mono")
    assert filename == "random_upload_id_译文.pdf"


def test_http_download_header_contains_safe_utf8_title_and_resumable_body(export_container):
    container, doc, _ = export_container
    doc.pages[0].blocks[0].spans[0].text = "合成标题：A/B"
    container.pipeline.deps.artifacts.path("0123456789abcdef", "ir.json").write_text(
        doc.model_dump_json(), encoding="utf-8"
    )
    client = TestClient(create_app(container))
    response = client.get("/api/jobs/0123456789abcdef/export?mode=dual", headers={"Range": "bytes=0-3"})
    assert response.status_code == 206 and response.content == b"%PDF"
    disposition = response.headers["content-disposition"]
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == "合成标题_A_B_双语对照.pdf"
