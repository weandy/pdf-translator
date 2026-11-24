import pytest

from pdftr.parsing.mineru_normalize import normalize_checked


@pytest.mark.parametrize("index", [True, False, -1, 2, "0", None, 0.0])
def test_shard_rejects_invalid_indexes(index):
    with pytest.raises(ValueError, match="页号"):
        normalize_checked(
            {"pdf_info": [{"page_idx": index, "page_size": [100, 100]}]},
            orig_pages=[9, 10],
            strict_relative=True,
        )


@pytest.mark.parametrize("size", [[0, 100], [10, float("nan")], [False, 100], [1]])
def test_invalid_page_sizes(size):
    with pytest.raises(ValueError, match="尺寸"):
        normalize_checked({"pdf_info": [{"page_idx": 0, "page_size": size}]})


def test_missing_and_duplicate_indexes():
    info = {"page_size": [100, 100]}
    with pytest.raises(ValueError, match="页号"):
        normalize_checked({"pdf_info": [info]})
    info["page_idx"] = 0  # type: ignore[index]
    with pytest.raises(ValueError, match="页号"):
        normalize_checked({"pdf_info": [info, info]})


def test_relative_remapping_does_not_shift_over_empty_page():
    layout = {"pdf_info": [{"page_idx": p, "page_size": [100, 100]} for p in (2, 0)]}
    result = normalize_checked(layout, orig_pages=[0, 10, 11], strict_relative=True)
    assert [page.index for page in result.pages] == [0, 11]
