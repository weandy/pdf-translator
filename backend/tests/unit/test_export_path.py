from pathlib import Path

import pymupdf
import pytest

from pdftr.domain.ir import Document
from pdftr.rendering.background import make_background
from pdftr.rendering.export import export_pdf_to_path


@pytest.mark.parametrize(("mode", "width"), [("mono", 100), ("dual", 200)])
def test_export_saves_pdf_without_serializing_to_python_bytes(tmp_path, monkeypatch, mode, width):
    original = tmp_path / "original.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=100, height=100).insert_text((10, 30), "Synthetic content")
        doc.save(original)

    def unexpected(*_args, **_kwargs):
        pytest.fail("导出不应生成完整的 Python PDF 字节串")

    monkeypatch.setattr(pymupdf.Document, "tobytes", unexpected)
    out = tmp_path / "exports" / "output.pdf"
    export_pdf_to_path(original, original, Document(job_id="synthetic", pages=[]), [], mode, links=[], out=out)
    with pymupdf.open(out) as doc:
        assert len(doc) == 1 and doc[0].rect.width == width
    assert list(out.parent.iterdir()) == [out]


@pytest.mark.parametrize("kind", ["export", "background"])
def test_failed_save_keeps_published_pdf_and_cleans_part(tmp_path, monkeypatch, kind):
    original = tmp_path / "original.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(original)
    out = tmp_path / "output.pdf"
    out.write_bytes(b"existing PDF")

    def interrupted(_doc, part: Path, **kwargs):
        assert part != out
        assert kwargs == {"garbage": 3, "deflate": True}
        part.write_bytes(b"unfinished")
        raise OSError("synthetic disk error")

    monkeypatch.setattr(pymupdf.Document, "save", interrupted)
    with pytest.raises(OSError, match="synthetic disk error"):
        if kind == "export":
            export_pdf_to_path(
                original, original, Document(job_id="synthetic", pages=[]), [], "mono", links=[], out=out
            )
        else:
            make_background(original, [], out)
    assert out.read_bytes() == b"existing PDF"
    assert not list(tmp_path.glob("*.part"))
