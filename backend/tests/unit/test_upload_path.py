from pathlib import Path

import pytest

from pdftr.extraction.chars import page_count


def test_malformed_pdf_releases_path_before_propagating_failure(tmp_path: Path):
    path = tmp_path / "upload.part"
    path.write_bytes(b"%PDF-synthetic-invalid")
    with pytest.raises(ValueError, match="无法打开"):
        try:
            page_count(path)
        finally:
            path.unlink()
    assert not path.exists()
