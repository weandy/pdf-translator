"""首开阅读与下载并发时只提取一次，缓存损坏或写入失败可以恢复。"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Document
from pdftr.pipeline import usecases
from pdftr.pipeline.runner import Pipeline
from pdftr.rendering.links import VERSION, PdfLink


def fixture_pipeline(folder: Path) -> Pipeline:
    return cast(
        Pipeline, SimpleNamespace(deps=SimpleNamespace(artifacts=SimpleNamespace(path=lambda _id, name: folder / name)))
    )


def sample_link() -> PdfLink:
    return PdfLink(page=0, rect=Rect(x0=1, y0=2, x1=3, y1=4), uri="https://example.org", anchor="synthetic")


def test_concurrent_reader_and_export_share_link_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pipeline = fixture_pipeline(tmp_path)
    doc = Document(job_id="synthetic", pages=[])
    calls = 0
    started, release = threading.Event(), threading.Event()

    def extract(*_args):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(2)
        return [sample_link()]

    monkeypatch.setattr(usecases, "page_links", extract)
    with ThreadPoolExecutor(max_workers=6) as pool:
        first = pool.submit(usecases._raw_links, pipeline, "synthetic", doc, [])
        assert started.wait(2)
        others = [pool.submit(usecases._raw_links, pipeline, "synthetic", doc, []) for _ in range(5)]
        # 给重叠请求进入提取路径的机会；旧实现会同时运行多次 extract。
        time.sleep(0.05)
        release.set()
        results = [future.result(timeout=3) for future in [first, *others]]
    assert calls == 1
    assert all(result == [sample_link()] for result in results)
    assert usecases._raw_links(pipeline, "synthetic", doc, []) == [sample_link()]
    assert calls == 1
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("broken", ["[", "null", '[{"page": "invalid"}]'])
def test_corrupt_cache_is_rebuilt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str):
    cache = tmp_path / f"links.v{VERSION}.json"
    cache.write_text(broken, encoding="utf-8")
    monkeypatch.setattr(usecases, "page_links", lambda *_args: [sample_link()])
    result = usecases._raw_links(fixture_pipeline(tmp_path), "synthetic", Document(job_id="synthetic", pages=[]), [])
    assert result == [sample_link()]
    assert json.loads(cache.read_text(encoding="utf-8"))[0]["anchor"] == "synthetic"


def test_failed_cache_publish_does_not_leave_a_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(usecases, "page_links", lambda *_args: [sample_link()])

    def fail_replace(*_args):
        raise OSError("synthetic publish failure")

    pipeline, doc = fixture_pipeline(tmp_path), Document(job_id="synthetic", pages=[])
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="synthetic publish failure"):
            usecases._raw_links(pipeline, "synthetic", doc, [])
    assert not list(tmp_path.glob("links.*"))
    assert usecases._raw_links(pipeline, "synthetic", doc, []) == [sample_link()]
