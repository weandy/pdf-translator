"""用合成文档验证译文导出及各入口的术语与翻译接线。"""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pymupdf
import pytest
from fastapi.testclient import TestClient

from pdftr import cli
from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, DocMeta, Document, Page, Segment, SegmentPart, SegmentStatus
from pdftr.domain.job import Job, JobOptions, JobStatus
from pdftr.domain.llm import ChatResult
from pdftr.glossary.matcher import TermMatcher
from pdftr.pipeline import runner, usecases
from pdftr.pipeline.secrets import Secrets
from pdftr.rendering.export import export_pdf, export_pdf_to_path, place_links
from pdftr.rendering.links import PdfLink
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore
from pdftr.translation.service import translate

JOB_ID = "0123456789abcdef"


def _block(page, order, bbox, size=10):
    return Block(
        id=f"p{page}-b{order}",
        page=page,
        order=order,
        bbox=Rect.of(bbox),
        type=BlockType.TEXT,
        style=BlockStyle(font_size=size),
        translatable=True,
    )


def _source_pdf(path, pages):
    with pymupdf.open() as pdf:
        for page in pages:
            pdf.new_page(width=page.width, height=page.height).insert_text((12, 40), "Synthetic")
        pdf.save(path)


@pytest.mark.parametrize("mode", ["mono", "dual"])
def test_export_reports_sorted_unique_overflow_for_actual_translation_loss(tmp_path, mode):
    original = tmp_path / "source.pdf"
    output = tmp_path / "export.pdf"
    long_line = "正文" * 100 + "结尾"
    outside = _block(0, 0, [410, 10, 460, 30])
    truncated = _block(0, 1, [20, 65, 90, 78])
    squeezed = _block(1, 0, [20, -5.65, 140, -0.65], size=15)
    neighbor = _block(1, 1, [20, 6.1, 140, 19])
    fallback = _block(1, 2, [20, 65, 90, 78])
    last = _block(2, 0, [410, 10, 460, 30])
    doc = Document(
        job_id=JOB_ID,
        pages=[
            Page(index=0, width=220, height=120, blocks=[outside, truncated]),
            Page(index=1, width=220, height=120, blocks=[squeezed, neighbor, fallback]),
            Page(index=2, width=220, height=120, blocks=[last]),
        ],
    )
    _source_pdf(original, doc.pages)
    segments = [
        Segment(
            id=block.id,
            job_id=JOB_ID,
            block_ids=[block.id],
            source=source,
            target=target,
            parts=[SegmentPart(block_id=block.id, chars=len(source))],
        )
        for block, source, target in [
            (last, "Source", "框外文字"),
            (fallback, long_line, ""),
            (squeezed, "Source", "仅一行"),
            (outside, "Source", "框外文字"),
            (truncated, "Source", long_line),
        ]
    ]

    assert export_pdf_to_path(original, original, doc, segments, mode, links=[], out=output) == [1, 3]
    with pymupdf.open(output) as pdf:
        assert len(pdf) == 3
        assert "框外文字" not in pdf[0].get_text()
        assert "结尾" not in pdf[0].get_text()
        assert "仅一行" in pdf[1].get_text()
        assert "结尾" not in pdf[1].get_text()
        assert "框外文字" not in pdf[2].get_text()


@pytest.mark.parametrize("side", ["left", "right"])
def test_cross_page_url_and_citation_keep_dual_link_targets(tmp_path, side):
    blocks = [_block(i, 0, [20, 50, 270, 100]) for i in range(2)]
    doc = Document(
        job_id=JOB_ID,
        pages=[Page(index=i, width=300 + 20 * i, height=150, blocks=[block]) for i, block in enumerate(blocks)],
    )
    original = tmp_path / "links.pdf"
    _source_pdf(original, doc.pages)
    url = "https://example.test:8443/path?q=1"
    bounds = Rect.of([25, 55, 55, 65])
    raw = [
        PdfLink(page=0, rect=bounds, block_id=blocks[0].id, uri=url, anchor=url),
        PdfLink(
            page=0, rect=bounds, block_id=blocks[0].id, kind="cite", anchor="12", dest_page=0, dest_x=30, dest_y=70
        ),
    ]
    seg = Segment(
        id="cross-page",
        job_id=JOB_ID,
        block_ids=[block.id for block in blocks],
        source=f"See {url} and [12].",
        target=f"甲{url} 后文[<b>12</b>]完成",
        parts=[SegmentPart(block_id=block.id, chars=weight) for block, weight in zip(blocks, [1, 3], strict=True)],
    )
    placed = place_links(raw, doc, [seg])
    assert [(link.block_id, link.text) for link in placed] == [(blocks[1].id, url), (blocks[1].id, "12")]
    assert all(link.block_id == blocks[0].id for link in raw)

    data = export_pdf(original, original, doc, [seg], "dual", side=side, links=raw)
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        for page_idx, link_side in enumerate(("left" if side == "right" else "right", side)):
            links = pdf[page_idx].get_links()
            uri_links = [link for link in links if link["kind"] == pymupdf.LINK_URI and link["uri"] == url]
            assert uri_links
            assert all((link["from"].x0 >= doc.pages[page_idx].width) == (link_side == "right") for link in uri_links)
            (cite,) = [link for link in links if link["kind"] == pymupdf.LINK_GOTO]
            assert cite["page"] == 0
            assert cite["to"].x == pytest.approx((300 if link_side == "right" else 0) + 30)
            assert (cite["from"].x0 >= doc.pages[page_idx].width) == (link_side == "right")


@pytest.fixture
def stored_job(tmp_path):
    container = build_container(Settings(data_dir=tmp_path, glossary_sources="", llm_concurrency=3))
    deps = container.pipeline.deps
    deps.store.save_job(Job(id=JOB_ID, filename="synthetic.pdf", status=JobStatus.DONE, stage="done"))
    doc = Document(job_id=JOB_ID, pages=[])
    deps.artifacts.path(JOB_ID, "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
    yield container
    assert isinstance(deps.store, SqliteStore)
    assert isinstance(deps.cache, SqliteTranslationCache)
    deps.store.close()
    deps.cache.close()


@pytest.fixture
def cached_export(stored_job, monkeypatch):
    renders = []

    def render(*args, out):
        renders.append(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF synthetic")
        return [3, 7]

    monkeypatch.setattr(usecases, "export_pdf_to_path", render)
    monkeypatch.setattr(usecases, "_raw_links", lambda *args: [])
    return stored_job, renders


@pytest.mark.parametrize(
    "invalid_meta",
    [
        None,
        b"{",
        b"\xff",
        "[]",
        '{"v":12,"overflow_pages":[]}',
        '{"v":13,"overflow_pages":[true]}',
        '{"v":13,"overflow_pages":[0]}',
        '{"v":13,"overflow_pages":[7,3]}',
        '{"v":13,"overflow_pages":[3,3]}',
        '{"v":13,"overflow_pages":[3.0]}',
        '{"v":13}',
    ],
)
def test_export_rebuilds_invalid_pair_and_reuses_repaired_cache(cached_export, invalid_meta):
    container, renders = cached_export
    pipeline = container.pipeline
    snapshot, _ = usecases._lookup_export(pipeline, JOB_ID, "mono", "right")
    pdf = snapshot.path
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF stale")
    meta = pdf.with_suffix(".meta.json")
    if invalid_meta is not None:
        meta.write_bytes(invalid_meta if isinstance(invalid_meta, bytes) else invalid_meta.encode())

    assert usecases.export_job(pipeline, JOB_ID, "mono") == ("synthetic_译文.pdf", pdf)
    assert json.loads(meta.read_text(encoding="utf-8")) == {"v": 13, "overflow_pages": [3, 7]}
    assert usecases.export_job(pipeline, JOB_ID, "mono")[1] == pdf
    assert pdf.read_bytes() == b"%PDF synthetic" and renders == [pdf]


def test_concurrent_sync_exports_share_one_publish(cached_export, monkeypatch):
    container, renders = cached_export
    first_started, second_missed_cache, resume = threading.Event(), threading.Event(), threading.Event()
    render, lookup = usecases.export_pdf_to_path, usecases._lookup_export

    def block_render(*args, out):
        first_started.set()
        assert resume.wait(4)
        return render(*args, out=out)

    def observe_lookup(*args):
        snapshot, cached = lookup(*args)
        if first_started.is_set() and cached is None:
            second_missed_cache.set()
        return snapshot, cached

    monkeypatch.setattr(usecases, "export_pdf_to_path", block_render)
    monkeypatch.setattr(usecases, "_lookup_export", observe_lookup)
    with ThreadPoolExecutor(max_workers=2) as threads:
        first = threads.submit(usecases.export_job, container.pipeline, JOB_ID, "mono")
        try:
            assert first_started.wait(2)
            second = threads.submit(usecases.export_job, container.pipeline, JOB_ID, "mono")
            assert second_missed_cache.wait(2)
        finally:
            resume.set()
        assert first.result(timeout=3) == second.result(timeout=3)
    assert len(renders) == 1


@pytest.mark.parametrize("has_orphan_meta", [False, True])
def test_snapshot_is_downloadable_only_after_both_files_are_published(cached_export, monkeypatch, has_orphan_meta):
    container, renders = cached_export
    snapshot, _ = usecases._lookup_export(container.pipeline, JOB_ID, "mono", "right")
    if has_orphan_meta:
        snapshot.path.parent.mkdir()
        snapshot.path.with_suffix(".meta.json").write_text('{"v":13,"overflow_pages":[8]}', encoding="utf-8")
    root = f"/api/jobs/{JOB_ID}/export"
    url = f"{root}?mode=mono&side=right&snapshot={snapshot.digest}"
    pdf_published, resume = threading.Event(), threading.Event()
    write_meta = usecases._write_export_meta

    def block_publish(pdf, pages):
        pdf_published.set()
        assert resume.wait(4)
        write_meta(pdf, pages)

    monkeypatch.setattr(usecases, "_write_export_meta", block_publish)
    with TestClient(create_app(container)) as client, ThreadPoolExecutor(max_workers=1) as threads:
        prepare = threads.submit(client.post, f"{root}/prepare")
        try:
            assert pdf_published.wait(2)
            assert snapshot.path.is_file() and client.get(url).status_code == 404
        finally:
            resume.set()
        prepared = prepare.result(timeout=3)
        assert prepared.status_code == 200
        assert prepared.json()["download_url"] == url and prepared.json()["overflow_pages"] == [3, 7]
        ranged = client.get(url, headers={"Range": "bytes=0-3"})
        assert ranged.status_code == 206 and ranged.content == b"%PDF"
        assert client.get(url.replace("mode=mono", "mode=dual")).status_code == 404
        assert client.get(f"{root}?snapshot=invalid").status_code == 422
        assert client.get(root).content == b"%PDF synthetic"
    assert renders == [snapshot.path]


def test_old_pairs_and_expired_orphans_are_cleaned_with_commit_marker_first(cached_export, monkeypatch):
    container, _ = cached_export
    snapshot, _ = usecases._lookup_export(container.pipeline, JOB_ID, "mono", "right")
    folder = snapshot.path.parent
    folder.mkdir()
    old_pdf = folder / "mono-right-0000000000000000.pdf"
    old_meta = old_pdf.with_suffix(".meta.json")
    orphan = folder / "dual-left-0000000000000000.meta.json"
    partial = folder / ".mono-right-interrupted.part"
    fresh = folder / ".mono-right-in-progress.part"
    old = time.time() - 120
    for path in (old_pdf, old_meta, orphan, partial, fresh):
        path.touch()
        if path != fresh:
            os.utime(path, (old, old))
    removed = []
    unlink = type(old_pdf).unlink

    def record_unlink(path, *args, **kwargs):
        if path in (old_meta, old_pdf):
            removed.append(path)
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(old_pdf), "unlink", record_unlink)
    assert usecases.export_job(container.pipeline, JOB_ID, "mono")[1] == snapshot.path
    assert removed == [old_meta, old_pdf]
    assert not orphan.exists() and not partial.exists() and fresh.is_file()
    assert snapshot.path.with_suffix(".meta.json").is_file()


def _text_segment(index, source, **kwargs):
    return Segment(
        id=f"s{index}", job_id=JOB_ID, block_ids=[f"p0-b{index}"], source=source, role=BlockType.TEXT, **kwargs
    )


def test_runner_and_retranslation_share_context_and_concurrency(stored_job, monkeypatch):
    pipeline = stored_job.pipeline
    deps = pipeline.deps
    job = usecases.get_job(pipeline, JOB_ID)
    job.options = JobOptions(user_terms={"next page": "续页"})
    deps.store.save_job(job)
    doc = Document(job_id=JOB_ID, pages=[], meta=DocMeta(title="Synthetic title", abstract="Synthetic abstract"))
    deps.artifacts.path(JOB_ID, "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
    deps.artifacts.path(JOB_ID, "glossary.json").write_text(
        '{"domain":"物理","use_library":true,"terms":{"phase change":"相变"}}', encoding="utf-8"
    )
    edited = {"status": SegmentStatus.EDITED, "target": "旧译文", "quality": "careful"}
    segments = [
        _text_segment(0, "An earlier sentence. The switch controls the phase change.", section="Methods", **edited),
        _text_segment(1, "This switch is shown on the next page.", section="Methods"),
        _text_segment(2, "The validation ends.", section="Validation", **edited),
        _text_segment(3, "This conclusion is brief.", section="Validation", **edited),
    ]
    deps.store.replace_segments(JOB_ID, segments)
    prompts = []

    class Chat:
        model = "synthetic"

        async def complete(self, system, user, opts):
            prompts.append((system, user))
            if "1|This conclusion is brief." in user:
                return ChatResult(text="", finish_reason="refusal")
            return ChatResult(text="1|此开关见下一页。")

    calls = []

    async def observe_translate(selected, *, context_segments, opts, **kwargs):
        calls.append(([(s.id, s.status) for s in selected], [s.id for s in context_segments], opts))
        return await translate(selected, context_segments=context_segments, opts=opts, **kwargs)

    monkeypatch.setattr(
        pipeline,
        "deps",
        replace(deps, chat_factory=lambda _: Chat(), term_matcher=lambda: TermMatcher({"switch": "开关"})),
    )
    monkeypatch.setattr(runner, "translate", observe_translate)
    monkeypatch.setattr(usecases, "translate", observe_translate)
    secrets = Secrets("http://localhost:11434/v1", "test-key", "synthetic", "")
    asyncio.run(pipeline._translate(job, secrets))
    returned = asyncio.run(usecases.retranslate_segments(pipeline, JOB_ID, ["s3", "s1", "s3"], "default", secrets))

    (runner_targets, runner_context, runner_options), (retry_targets, retry_context, retry_options) = calls
    assert runner_context == retry_context == ["s0", "s1", "s2", "s3"]
    assert runner_targets == [(s.id, s.status) for s in segments]
    assert retry_targets == [("s3", SegmentStatus.PENDING), ("s1", SegmentStatus.PENDING)]
    assert runner_options.concurrency == retry_options.concurrency == 3
    assert retry_options.total_timeout_s == 45 and not retry_options.use_cache
    switch_prompts = [(system, user) for system, user in prompts if "1|This switch is shown" in user]
    assert len(switch_prompts) == 2
    for system, user in switch_prompts:
        assert "Synthetic title" in system and "Synthetic abstract" in system and "phase change=相变" in system
        assert "第1段前文（仅供理解指代，勿翻译）：The switch controls the phase change." in user
        assert "第1段用户术语（统一使用）：next page=续页" in user
        assert "第1段参考词库" in user and "switch=开关" in user
    assert any("第1段前文（仅供理解指代，勿翻译）：The validation ends." in user for _, user in prompts)
    assert [s.id for s in returned] == ["s3", "s1"]
    assert (returned[0].target, returned[0].status, returned[0].quality) == ("旧译文", SegmentStatus.EDITED, "careful")
    assert returned[0].failure_reason and returned[1].status is SegmentStatus.DONE
    revisions = {r.segment_id: r for r in usecases.revisions(pipeline, JOB_ID)}
    assert set(revisions) == {"s1", "s3"}
    assert revisions["s3"].before == revisions["s3"].after == "旧译文"
    assert revisions["s3"].action == "retranslate"


def test_job_terms_groups_spelling_variants_across_sources(stored_job, monkeypatch):
    pipeline = stored_job.pipeline
    segments = [_text_segment(i, f"Sample {i}") for i in range(3)]
    pipeline.deps.store.replace_segments(JOB_ID, segments)
    matches = {
        "Sample 0": ({}, {}, {"policy-gradient": "公共梯度", "fair value": "公共公允价值"}),
        "Sample 1": ({}, {"Policy gradients": "论文梯度", "fair-value": "公允价值"}, {}),
        "Sample 2": ({"policy gradient": "用户梯度", "Policy gradients": "用户梯度"}, {}, {}),
    }
    seen = []

    class Selection:
        def match_segment(self, source):
            seen.append(source)
            return matches[source]

    monkeypatch.setattr(usecases, "load_context", lambda *_: (None, Selection()))
    assert [row.model_dump() for row in usecases.job_terms(pipeline, JOB_ID)] == [
        {"source": "policy gradient", "target": "用户梯度", "origin": "user", "segment_ids": ["s0", "s1", "s2"]},
        {"source": "fair-value", "target": "公允价值", "origin": "paper", "segment_ids": ["s0", "s1"]},
    ]
    assert seen == [s.source for s in segments]


@pytest.mark.parametrize(("protocol", "configured", "expected"), [("openai", 0, 1), ("anthropic", 7, 7)])
def test_cli_uses_configured_chat_protocol_and_concurrency(tmp_path, monkeypatch, protocol, configured, expected):
    layout = tmp_path / "layout.json"
    layout.write_text("{}", encoding="utf-8")
    segments = [
        _text_segment(0, "A switch controls the phase change.", section="Methods"),
        _text_segment(1, "This switch is shown here.", section="Methods"),
    ]
    doc = Document(job_id=JOB_ID, pages=[], meta=DocMeta(title="Synthetic title"))
    settings = Settings(
        data_dir=tmp_path, llm_protocol=protocol, llm_concurrency=configured,
        llm_base_url="http://localhost:11434/v1", llm_api_key="test-key", llm_model="synthetic", glossary_sources="",
    )  # fmt: skip
    opened, prompts, closed = [], [], []

    class Chat:
        model = settings.llm_model

        async def complete(self, system, user, opts):
            prompts.append((system, user))
            if "抽取需要全文统一译法的专业术语" in user:
                return ChatResult(text="领域：物理\nAI公共词库：停用\nphase change=相变")
            return ChatResult(text="1|一个开关控制相变。\n2|这个开关如图所示。")

        async def aclose(self):
            closed.append(self)

    def factory(name):
        def create(**kwargs):
            opened.append((name, kwargs))
            return Chat()

        return create

    async def observe_translate(selected, *, chat, context_segments, opts, lookup_term_hits, **kwargs):
        assert selected == context_segments == segments
        assert lookup_term_hits(segments[0].source) == ({}, {"phase change": "相变"}, {})
        assert opts.concurrency == expected
        result = await translate(selected, chat=chat, context_segments=context_segments, opts=opts,
                                 lookup_term_hits=lookup_term_hits, **kwargs)  # fmt: skip
        assert result[1].translated == 2
        return result

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "normalize", lambda _: None)
    monkeypatch.setattr(cli, "extract", lambda _: [])
    monkeypatch.setattr(cli, "fuse", lambda *_args, **_kwargs: (doc, None))
    monkeypatch.setattr(cli, "segment", lambda *_args, **_kwargs: segments)
    monkeypatch.setattr(cli, "OpenAICompatChat", factory("openai"))
    monkeypatch.setattr(cli, "AnthropicChat", factory("anthropic"))
    monkeypatch.setattr(cli, "translate", observe_translate)
    cli.main(["translate", "synthetic.pdf", str(layout)])

    assert opened == [(protocol, {"base_url": settings.llm_base_url, "api_key": "test-key", "model": "synthetic"})]
    assert len(closed) == 1
    _, (system, user) = prompts
    assert "phase change=相变" in system
    assert "第2段前文（仅供理解指代，勿翻译）：A switch controls the phase change." in user
