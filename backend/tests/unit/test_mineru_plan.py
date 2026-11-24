import pymupdf
import pytest

from pdftr.parsing.mineru_plan import build_parse_plan, compute_requested_pages, materialize_shards
from pdftr.parsing.port import ParseError


def make_pdf(path, count, first=None):
    with pymupdf.open() as doc:
        for index in range(count):
            page = doc.new_page()
            if index == 0 and first:
                page.insert_text((72, 90), first)
        doc.save(path)


@pytest.mark.parametrize(
    ("count", "pages", "cover", "expected"),
    [
        (16, None, False, None),
        (16, list(range(16)), False, None),
        (6, [4], False, None),
        (16, list(range(2, 16)), True, None),
        (16, [12, 10, 10], False, (0, 10, 12)),
        (16, [12, 10, 10], True, (0, 1, 10, 12)),
    ],
)
def test_requested_pages(count, pages, cover, expected):
    assert compute_requested_pages(count, pages, cover) == expected


@pytest.mark.parametrize("pages", [[], [True], [-1], [16], ["1"]])
def test_invalid_selection(pages):
    with pytest.raises(ParseError, match="页码范围无效"):
        compute_requested_pages(16, pages, False)


def test_partial_plan_keeps_contiguous_runs(tmp_path):
    pdf = tmp_path / "src.pdf"
    make_pdf(pdf, 16)
    native = build_parse_plan(pdf, list(range(5)), enable_partial_pages=True)
    assert native.mode == "native_ranges"
    assert native.shards[0].page_ranges == "1-5"
    plan = build_parse_plan(pdf, list(range(9, 15)), enable_partial_pages=True)
    assert plan.mode == "local_shards"
    assert [s.orig_pages for s in plan.shards] == [(0,), tuple(range(9, 15))]
    _, files = materialize_shards(pdf, plan, tmp_path)
    for path, expected_count in zip(files, (1, 6), strict=True):
        with pymupdf.open(path) as doc:
            assert doc.page_count == expected_count
    fragmented = build_parse_plan(pdf, list(range(0, 16, 2)), enable_partial_pages=True)
    assert fragmented.mode == "native_ranges"
    assert fragmented.shards[0].page_ranges == "1,3,5,7,9,11,13,15"


def test_cover_and_large_file(tmp_path):
    pdf = tmp_path / "cover.pdf"
    make_pdf(pdf, 16, "Paper cover")
    cover = build_parse_plan(pdf, [10, 11, 12], enable_partial_pages=True)
    assert cover.requested_pages == (0, 1, 10, 11, 12)
    with pdf.open("r+b") as stream:
        # 在 PDF 前置合法注释中增加体积，同时保留原正文。
        original = stream.read()
        eof = original.rindex(b"startxref")
        stream.seek(eof)
        stream.write(b"%" + b"a" * (10 * 1024 * 1024) + b"\n" + original[eof:])
    plan = build_parse_plan(pdf, list(range(5)), enable_partial_pages=True)
    assert plan.mode == "local_shards"
    assert plan.shards[0].orig_pages == tuple(range(5))


def test_natural_boundary_after_complete_sentence_and_before_heading(tmp_path):
    pdf = tmp_path / "natural.pdf"
    with pymupdf.open() as doc:
        for index in range(30):
            page = doc.new_page()
            head = "2 Results" if index == 16 else "continuation of the previous example"
            tail = "This example ends a full sentence at this boundary." if index == 15 else "running sentence,"
            page.insert_text((72, 100), head)
            page.insert_text((72, 250), tail)
        doc.save(pdf)
    plan = build_parse_plan(pdf, None, enable_sharding=True)
    assert [len(shard.orig_pages) for shard in plan.shards] == [16, 14]


def test_no_text_uses_ideal_boundaries_and_bad_pdfs_fail(tmp_path):
    pdf = tmp_path / "scan.pdf"
    make_pdf(pdf, 52)
    plan = build_parse_plan(pdf, None, enable_sharding=True)
    assert [len(x.orig_pages) for x in plan.shards] == [13, 13, 13, 13]
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"invalid")
    with pytest.raises(ParseError, match="无法打开"):
        build_parse_plan(bad, None, enable_sharding=True)
    encrypted = tmp_path / "encrypted.pdf"
    encryption = "PDF_ENCRYPT_AES_256"
    with pymupdf.open(pdf) as doc:
        doc.save(encrypted, encryption=getattr(pymupdf, encryption), user_pw="test")
    with pytest.raises(ParseError, match="已加密"):
        build_parse_plan(encrypted, None, enable_sharding=True)


def test_cut_size_guard_reverts_to_original(tmp_path, monkeypatch):
    import pdftr.parsing.mineru_plan as planner

    pdf = tmp_path / "long.pdf"
    make_pdf(pdf, 30)
    plan = build_parse_plan(pdf, None, enable_sharding=True)
    monkeypatch.setattr(planner, "MIB", 1)
    fallback, paths = materialize_shards(pdf, plan, tmp_path)
    assert fallback.mode == "full_single"
    assert paths == (pdf,)
    assert list(tmp_path.iterdir()) == [pdf]


@pytest.mark.parametrize("rotation", [90, 270])
def test_rotated_page_uses_visible_page_geometry(rotation):
    from pdftr.parsing.mineru_plan import _boundary_lines

    with pymupdf.open() as doc:
        page = doc.new_page(width=300, height=500)
        body = "Visible body text at the middle of the rotated page."
        page.insert_text((245, 485), body, rotate=90)
        page.set_rotation(rotation)
        assert _boundary_lines(doc, 0, {}) == (body, body)


def test_long_separate_runs_share_shard_budget_proportionally(tmp_path):
    pdf = tmp_path / "multiple.pdf"
    make_pdf(pdf, 80)
    selected = [*range(26), *range(36, 74)]
    plan = build_parse_plan(pdf, selected, enable_partial_pages=True, enable_sharding=True, max_shards=5)
    assert [shard.orig_pages for shard in plan.shards] == [
        tuple(range(a, b)) for a, b in [(0, 13), (13, 26), (36, 49), (49, 61), (61, 74)]
    ]
