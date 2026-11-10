"""上传页数校验直接使用内存流，同时保留已有 Path 入口。"""

from pathlib import Path

import pymupdf
import pytest

from pdftr.extraction.chars import page_count


def synthetic_pdf(pages: int = 3, *, encrypted: bool = False) -> bytes:
    with pymupdf.open() as doc:
        for _ in range(pages):
            doc.new_page()
        return doc.tobytes(
            encryption=getattr(pymupdf, "PDF_ENCRYPT_AES_256" if encrypted else "PDF_ENCRYPT_NONE"),
            user_pw="synthetic-user" if encrypted else None,
            owner_pw="synthetic-owner" if encrypted else None,
        )


def test_page_count_accepts_bytes_and_path_and_closes_successful_file(tmp_path: Path):
    data = synthetic_pdf()
    assert page_count(data) == 3
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(data)
    assert page_count(path) == 3
    path.unlink()  # Windows 会拒绝删除仍由 PyMuPDF 打开的文件。
    assert not path.exists()


def test_invalid_pdf_is_checked_as_stream_without_opening_temporary_file(monkeypatch):
    original = pymupdf.open
    streams = []

    def open_stream(*args, **kwargs):
        assert not args and isinstance(kwargs.get("stream"), bytes)
        streams.append(kwargs["stream"])
        return original(*args, **kwargs)

    monkeypatch.setattr(pymupdf, "open", open_stream)
    for _ in range(3):
        with pytest.raises(ValueError, match="无法打开"):
            page_count(b"%PDF-1.4 broken")
    assert len(streams) == 3


def test_encrypted_pdf_is_rejected_in_memory():
    with pytest.raises(ValueError, match="已加密"):
        page_count(synthetic_pdf(encrypted=True))
