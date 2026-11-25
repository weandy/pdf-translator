"""合成 PDF 的补翻 API、版本隔离与阶段故障回归。"""

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import Container, build_container
from pdftr.domain.expansion import ExpansionOp, ExpansionStage, ExpansionStatus
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType, PageKind, SegmentStatus
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.domain.llm import ChatOptions, ChatResult, ChatUsage
from pdftr.parsing.port import ParseProgress
from pdftr.pipeline import expansion, usecases
from pdftr.pipeline.runner import _layout_cache_key, load_document
from pdftr.pipeline.secrets import Secrets
from pdftr.pipeline.versions import candidate_dir, version_artifact
from pdftr.storage import expansions

HEADERS = {"X-LLM-Key": "fixture-key", "X-Parser-Key": "fixture-token", "X-LLM-Model": "deepseek-flash"}
PAGE_COUNT = 8
CONTINUATIONS = {3: "and the evidence follows on the next page.", 6: "with a final observation about the results."}


def _line(text: str, y: int, *, formula: bool = False) -> LayoutLine:
    box = Rect.of((50, y - 12, 520, y + 4))
    spans = [LayoutSpan(kind="text", content=text, bbox=box)]
    if formula:
        spans = [
            LayoutSpan(kind="text", content="The expression", bbox=Rect.of((50, y - 12, 120, y + 4))),
            LayoutSpan(kind="inline_formula", content="f(x)", bbox=Rect.of((124, y - 11, 143, y + 3))),
            LayoutSpan(kind="text", content="is stable.", bbox=Rect.of((154, y - 12, 220, y + 4))),
        ]
    return LayoutLine(bbox=box, spans=spans)


def _block(text: str, y: int, order: int, *, kind: BlockType = BlockType.TEXT) -> LayoutBlock:
    line = _line(text, y)
    return LayoutBlock(type=kind, bbox=line.bbox, order=order, lines=[line])


def synthetic_paper() -> tuple[bytes, LayoutResult]:
    """八页包含两处跨页续段、扫描页、带坐标的公式及页眉页脚。"""
    layout: list[LayoutPage] = []
    with pymupdf.open() as doc:
        for index in range(PAGE_COUNT):
            page = doc.new_page(width=600, height=800)
            body = f"Independent evidence from section number {index + 1}."
            blocks = [
                _block("Running header", 20, 0, kind=BlockType.HEADER),
                _block(body, 160, 2),
                _block(f"Page {index + 1}", 775, 20, kind=BlockType.FOOTER),
            ]
            for x, y, text in ((50, 20, "Running header"), (50, 160, body), (50, 775, f"Page {index + 1}")):
                page.insert_text((x, y), text, fontsize=10)
            if index == 0:
                page.insert_text((50, 90), "A Study of Incremental Translation", fontsize=16)
                blocks.append(_block("A Study of Incremental Translation", 90, 1, kind=BlockType.TITLE))
            if index in (1, 4):
                for x, text in ((50, "The expression"), (124, "f(x)"), (154, "is stable.")):
                    page.insert_text((x, 260), text, fontsize=10)
                line = _line("", 260, formula=True)
                blocks.append(LayoutBlock(type=BlockType.TEXT, bbox=line.bbox, order=3, lines=[line]))
            if index + 1 in CONTINUATIONS:
                start = "The method remains effective"
                page.insert_text((50, 710), start, fontsize=10)
                block = _block(start, 710, 8)
                block.lines.append(_line(CONTINUATIONS[index + 1], 65))
                blocks.append(block)
            if index in CONTINUATIONS:
                continuation = CONTINUATIONS[index]
                page.insert_text((50, 65), continuation, fontsize=10)
                blocks.append(
                    LayoutBlock(
                        type=BlockType.TEXT,
                        bbox=_line(continuation, 65).bbox,
                        order=1,
                        placeholder=True,
                        merge_prev=True,
                    )
                )
            if index == PAGE_COUNT - 1:
                # 栅格化整页，避免测试拿带原生文字的图片页冒充扫描页。
                image = page.get_pixmap().tobytes("png")
                page.add_redact_annot(page.rect)
                page.apply_redactions()
                page.insert_image(page.rect, stream=image)
            layout.append(LayoutPage(index=index, width=600, height=800, blocks=sorted(blocks, key=lambda b: b.order)))
        return doc.tobytes(), LayoutResult(parser="mineru-cloud", pages=layout)


class Parser:
    name = "fake"
    is_cloud = True
    cache_config_sig = "expansion-test"

    def __init__(self, layout: LayoutResult) -> None:
        self.layout = layout
        self.calls: list[list[int] | None] = []
        self.requested_none = False
        self.paused = False
        self.started = threading.Event()

    async def parse(
        self,
        pdf: Path,
        pages: list[int] | None = None,
        raw_dir: Path | None = None,
        on_progress: ParseProgress | None = None,
    ) -> LayoutResult:
        self.calls.append(pages)
        if on_progress:
            on_progress("正在解析合成样本", 0.5)
        self.started.set()
        while self.paused:
            await asyncio.sleep(0.02)
        return self.layout.model_copy(
            update={
                "pages": [p for p in self.layout.pages if pages is None or p.index in pages],
                "requested_pages": None if self.requested_none else pages,
            },
            deep=True,
        )


class Chat:
    model = "deepseek-flash"

    def __init__(self) -> None:
        self.sources: list[str] = []

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        out = []
        for line in user.splitlines():
            if match := re.match(r"^(\d+)\|(.*)$", line):
                self.sources.append(match[2])
                text = re.sub(r"<(m\d+)>.*?</\1>", r"<\1/>", match[2])
                formulas = " ".join(re.findall(r"<m\d+/>", text))
                number = re.search(r"number (\d+)", text)
                label = f"第 {number[1]} 节的独立证据。" if number else "合成段落的中文译文。"
                out.append(f"{match[1]}|{label} {formulas}")
        return ChatResult(
            text="\n".join(out) if out else "领域：测试",
            usage=ChatUsage(prompt_tokens=100, completion_tokens=40),
        )


def make_fixture(path: Path) -> tuple[Container, bytes, Parser, Chat]:
    pdf, layout = synthetic_paper()
    parser, chat = Parser(layout), Chat()
    settings = Settings(data_dir=path, glossary_sources="", font_dir=path / "fonts")
    container = build_container(
        settings, {"parser_factory": lambda sec, name: parser, "chat_factory": lambda sec: chat}
    )
    return container, pdf, parser, chat


@pytest.fixture
def app(tmp_path: Path) -> Iterator[tuple[TestClient, Container, bytes, Parser, Chat]]:
    container, pdf, parser, chat = make_fixture(tmp_path)
    with TestClient(create_app(container)) as client:
        yield client, container, pdf, parser, chat
    container.close()


def _until(client: TestClient, url: str, predicate) -> dict:
    deadline = time.monotonic() + 10
    data: dict = {}
    while time.monotonic() < deadline:
        response = client.get(url)
        assert response.status_code == 200, response.text
        data = response.json()
        if predicate(data):
            return data
        time.sleep(0.015)
    pytest.fail(f"等待 {url} 超时：{data}")


def _upload(client: TestClient, pdf: bytes, pages: list[int]) -> str:
    response = client.post(
        "/api/jobs",
        headers=HEADERS,
        files={"file": ("synthetic.pdf", pdf, "application/pdf")},
        data={"options": json.dumps({"cloud_consent": True, "pages": pages})},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    job = _until(client, f"/api/jobs/{job_id}", lambda j: j["status"] in ("done", "failed"))
    assert job["status"] == "done", job["error"]
    return job_id


def _start(client: TestClient, job_id: str, pages: list[int] | None = None, headers=HEADERS) -> str:
    response = client.post(f"/api/jobs/{job_id}/expand-pages", json={"pages": pages}, headers=headers)
    assert response.status_code == 202, response.text
    return f"/api/jobs/{job_id}/expansions/{response.json()['id']}"


def _wait(client: TestClient, url: str) -> dict:
    return _until(client, url, lambda op: op["status"] in ("done", "failed", "cancelled"))


def _rows(container: Container, job_id: str) -> tuple[dict, list, tuple]:
    with sqlite3.connect(container.settings.data_dir / "app.db") as db:
        return (
            {
                sid: (status, data)
                for sid, status, data in db.execute("SELECT id, status, data FROM segments WHERE job_id = ?", (job_id,))
            },
            db.execute("SELECT * FROM revisions WHERE job_id = ? ORDER BY id", (job_id,)).fetchall(),
            (
                db.execute("SELECT completed FROM analytics_jobs WHERE job_id = ?", (job_id,)).fetchone(),
                db.execute("SELECT count(*) FROM analytics_failures").fetchone(),
            ),
        )


def _assert_old_download(client: TestClient, job_id: str, expected_pdf: bytes) -> None:
    root = f"/api/jobs/{job_id}"
    current = client.get(root).json()
    assert current["status"] == "done" and current["content_version"] == 1
    assert client.get(f"{root}/document").json()["job"]["content_version"] == 1
    assert client.get(f"{root}/files/background.pdf?v=1").content == expected_pdf
    assert client.get(f"{root}/files/background.pdf?v=2").status_code == 404
    assert client.get(f"{root}/files/background.pdf").headers["Cache-Control"] == "private, no-cache"
    assert client.get(f"{root}/export?mode=mono").content.startswith(b"%PDF")


@pytest.mark.parametrize("initial", [[0, 1, 2], [3, 4, 5]])
def test_expansion_preserves_edited_rows_and_formula_crop(app, initial):
    client, container, pdf, parser, chat = app
    job_id = _upload(client, pdf, initial)
    root = f"/api/jobs/{job_id}"
    pipeline = container.pipeline
    prior = pipeline.deps.store.segments(job_id)
    formula_seg = next(seg for seg in prior if seg.formula_refs)
    png_url = f"/api/segments/{formula_seg.id}/formulas/m1.png"
    png = client.get(png_url)
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")
    edit = client.patch(f"/api/segments/{formula_seg.id}", json={"target": "已手动修改 <m1/>"})
    assert edit.status_code == 200, edit.text
    old_failed = next(seg for seg in prior if f"number {initial[0] + 1}." in seg.source)
    old_failed.status, old_failed.target = SegmentStatus.FAILED, None
    pipeline.deps.store.save_segments([old_failed])
    old_rows, revisions, analytics = _rows(container, job_id)
    doc_before = load_document(pipeline.deps, job_id)
    assert len(revisions) == 1
    export0 = client.post(f"{root}/export/start?mode=mono").json()
    export0 = _until(
        client,
        f"{root}/export/status?mode=mono&snapshot={export0['snapshot']}",
        lambda state: state["status"] == "ready",
    )
    original_background = client.get(f"{root}/files/background.pdf?v=1").content
    chat.sources.clear()
    if initial[0] == 3:
        op = _wait(client, _start(client, job_id, [0, 1, 2, 3]))
        assert op["status"] == "done", op
        assert op["requested_pages"] == [0, 1, 2]
        assert client.get(root).json()["published_pages"] == list(range(6))
        assert client.get(f"{root}/files/background.pdf?v=1").content == original_background
        doc_middle = load_document(pipeline.deps, job_id)
    else:
        doc_middle = None
    op = _wait(client, _start(client, job_id))
    assert op["status"] == "done", op
    current = client.get(root).json()
    assert current["status"] == "done" and current["published_pages"] == list(range(PAGE_COUNT))
    assert current["content_version"] == (3 if doc_middle else 2)
    assert current["options"]["pages"] is None
    final_doc = load_document(pipeline.deps, job_id)
    assert final_doc.pages[-1].kind is PageKind.SCANNED
    assert all(final_doc.pages[i] == doc_before.pages[i] for i in initial)
    if doc_middle:
        assert final_doc.pages[:6] == doc_middle.pages[:6]
    assert client.get(png_url).content == png.content
    rows, final_revisions, final_analytics = _rows(container, job_id)
    assert {sid: rows[sid] for sid in old_rows} == old_rows
    assert final_revisions == revisions and final_analytics == analytics == ((1,), (0,))
    segs = pipeline.deps.store.segments(job_id)
    order = {block.id: idx for idx, block in enumerate(final_doc.blocks())}
    assert [order[s.block_ids[0]] for s in segs] == sorted(order[s.block_ids[0]] for s in segs)
    assert sorted(int(s.id.rsplit("-s", 1)[1]) for s in segs) == list(range(len(segs)))
    for tail in CONTINUATIONS.values():
        assert sum(s.source.count(tail) for s in segs) == 1
    assert all(f"section number {index + 1}." not in "\n".join(chat.sources) for index in initial)
    assert client.get(export0["download_url"]).content.startswith(b"%PDF")
    with pymupdf.open(stream=client.get(f"{root}/export?mode=mono").content) as output:
        assert len(output) == PAGE_COUNT
        assert all(
            "Independent evidence" not in output[index].get_text()
            for index in range(PAGE_COUNT - 1)
            if index != initial[0]
        )
    assert parser.calls[-1] == ([6, 7] if initial[0] == 3 else [3, 4, 5, 6, 7])


def test_forward_expansion_strips_short_covered_tail_and_tightens_remaining_box(app):
    client, container, pdf, parser, chat = app
    long_line = "and the evidence follows in"
    short_line = "Table 2."
    remaining = "An independent paragraph on the new page."
    source = next(block for block in parser.layout.pages[2].blocks if block.order == 8)
    source.lines[1:] = [_line(long_line, 65), _line(short_line, 85)]
    target = next(block for block in parser.layout.pages[3].blocks if block.placeholder)
    target.placeholder = target.merge_prev = False
    target.lines = [*source.lines[1:], _line(remaining, 105)]
    target.bbox = Rect.of((50, 53, 520, 109))
    with pymupdf.open(stream=pdf) as paper:
        page = paper[3]
        page.add_redact_annot(pymupdf.Rect(48, 52, 522, 69))
        page.apply_redactions()
        for y, text in ((65, long_line), (85, short_line), (105, remaining)):
            page.insert_text((50, y), text, fontsize=10)
        pdf = paper.tobytes()

    job_id = _upload(client, pdf, [2])
    deps = container.pipeline.deps
    frozen = load_document(deps, job_id).pages[2]
    assert any(short_line in seg.source for seg in deps.store.segments(job_id))
    chat.sources.clear()
    op = _wait(client, _start(client, job_id, [3]))
    assert op["status"] == "done", op
    doc = load_document(deps, job_id)
    assert doc.pages[2] == frozen
    block = next(block for block in doc.pages[3].blocks if block.order == target.order)
    assert block.bbox == target.lines[-1].bbox
    assert any(remaining in source_text for source_text in chat.sources)
    assert all(long_line not in source_text and short_line not in source_text for source_text in chat.sources)
    assert sum(seg.source.count(short_line) for seg in deps.store.segments(job_id)) == 1


@pytest.mark.parametrize("full_cache", [False, True])
def test_forward_expansion_only_moves_uncovered_lines_from_nonfinal_frozen_block(app, full_cache):
    client, container, pdf, parser, chat = app
    source = parser.layout.pages[2]
    source.blocks[1].order = 9
    source.blocks.sort(key=lambda block: block.order)
    deps = container.pipeline.deps
    tail = CONTINUATIONS[3]
    if full_cache:
        cache_job = _upload(client, pdf, [0])
        scope = hashlib.sha256(b"pdftr-layout-scope-v1:fixture-token").hexdigest()
        cache = deps.layout_cache
        assert cache is not None
        cache.put(
            _layout_cache_key(scope, hashlib.sha256(pdf).hexdigest(), parser.cache_config_sig, None),
            parser.layout,
            job_id=cache_job,
            ttl_seconds=3600,
        )

    job_id = _upload(client, pdf, [2])
    frozen = load_document(deps, job_id).pages[2]
    initial_sources = [seg.source for seg in deps.store.segments(job_id)]
    assert any(tail in text for text in initial_sources) is not full_cache
    chat.sources.clear()
    op = _wait(client, _start(client, job_id, [3]))
    assert op["status"] == "done", op
    assert parser.calls == ([[0]] if full_cache else [[2], [3]])
    assert load_document(deps, job_id).pages[2] == frozen
    assert any("number 4." in text for text in chat.sources)
    assert any(tail in text for text in chat.sources) is full_cache
    assert sum(seg.source.count(tail) for seg in deps.store.segments(job_id)) == 1


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
def test_expansion_releases_memory_when_candidate_cleanup_fails(app, monkeypatch, error):
    client, container, pdf, _parser, _chat = app
    pipeline = container.pipeline
    job_id = _upload(client, pdf, [0])
    job = pipeline.deps.store.get_job(job_id)
    assert job is not None
    op = ExpansionOp(
        id="cleanup", job_id=job_id, base_version=1, target_version=2, requested_pages=[1], merged_pages=[0, 1]
    )
    released = []

    async def execute(_run):
        return job

    def discard(_work):
        raise error()

    monkeypatch.setattr(expansion._Run, "execute", execute)
    monkeypatch.setattr(expansion, "discard_candidate", discard)
    monkeypatch.setattr(expansion, "release_memory", lambda: released.append(True))
    with pytest.raises(error):
        asyncio.run(expansion.run_expansion(pipeline, job, op, Secrets("", "", "", "")))
    assert released == [True]


@pytest.mark.parametrize("full_cache", [True, False])
def test_full_cache_only_merges_missing_pages_even_with_requested_none(app, full_cache):
    client, container, pdf, parser, _chat = app
    parser.requested_none = True
    job_id = _upload(client, pdf, [3, 4, 5])
    deps = container.pipeline.deps
    old = LayoutResult.model_validate_json(deps.artifacts.path(job_id, "layout.json").read_bytes())
    assert old.requested_pages is None and len(old.pages) == 3
    scope = hashlib.sha256(b"pdftr-layout-scope-v1:fixture-token").hexdigest()
    cache = deps.layout_cache
    assert cache is not None
    hit = parser.layout.model_copy(deep=True)
    hit.pages[3].blocks[-1].order = 99
    if full_cache:
        parser.cache_config_sig += "-full"
        cache.put(
            _layout_cache_key(scope, hashlib.sha256(pdf).hexdigest(), parser.cache_config_sig, None),
            hit,
            job_id=job_id,
            ttl_seconds=3600,
        )
    op = _wait(client, _start(client, job_id, [0, 1, 2]))
    assert op["status"] == "done", op
    assert parser.calls == ([[3, 4, 5]] if full_cache else [[3, 4, 5], [0, 1, 2]])
    merged = LayoutResult.model_validate_json(version_artifact(deps.artifacts, job_id, 2, "layout.json").read_bytes())
    assert merged.pages[3:] == old.pages
    assert merged.attempted_pages == list(range(PAGE_COUNT))


@pytest.mark.parametrize("stage", ["parse", "fuse", "segment", "translate", "render", "publish"])
def test_stage_failure_keeps_old_publication(app, monkeypatch, stage):
    client, container, pdf, _parser, _chat = app
    job_id = _upload(client, pdf, [0, 1, 2])
    deps = container.pipeline.deps
    old_background = deps.artifacts.path(job_id, "background.pdf").read_bytes()
    expected = _rows(container, job_id)
    before = deps.store.get_job(job_id)
    assert before is not None

    def fail(*args, **kwargs):
        raise RuntimeError("injected boundary")

    async def fail_async(*args, **kwargs):
        fail()

    if stage == "publish":
        save = expansions.save_expansion

        def fail_commit(db, op):
            if op.status is ExpansionStatus.DONE:
                fail()
            save(db, op)

        monkeypatch.setattr(expansions, "save_expansion", fail_commit)
    elif stage == "parse":
        monkeypatch.setattr(expansion._Run, stage, fail_async)
    elif stage == "translate":
        monkeypatch.setattr(expansion, "translate", fail_async)
    else:
        monkeypatch.setattr(expansion, f"_stage_{stage}", fail)
    url = _start(client, job_id)
    op = _wait(client, url)
    assert (op["status"], op["stage"]) == ("failed", stage)
    _assert_old_download(client, job_id, old_background)
    assert _rows(container, job_id) == expected
    persisted = deps.store.expansion(job_id, op["id"])
    assert persisted is not None
    after = deps.store.get_job(job_id)
    assert after is not None
    if stage in ("render", "publish"):
        assert persisted.cost_cny is not None and persisted.cost_applied
        assert after.cost_cny == pytest.approx((before.cost_cny or 0) + persisted.cost_cny)
        deps.store.finish_expansion(job_id, op["id"], ExpansionStatus.FAILED, "duplicate")
        assert deps.store.get_job(job_id) == after
    else:
        assert after == before
    assert not version_artifact(deps.artifacts, job_id, 2, "ir.json").exists()


def test_cancel_mutation_and_export_lifecycle(app):
    client, container, pdf, parser, _chat = app
    job_id = _upload(client, pdf, [0, 1, 2])
    root = f"/api/jobs/{job_id}"
    pipeline = container.pipeline
    deps = pipeline.deps
    job = deps.store.get_job(job_id)
    assert job is not None
    job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    deps.store.save_job(job)
    seg = deps.store.segments(job_id)[0]
    assert client.patch(f"/api/segments/{seg.id}", json={"target": "原有编辑"}).status_code == 200
    revision = deps.store.revisions(job_id)[0]
    old_background = deps.artifacts.path(job_id, "background.pdf").read_bytes()
    parser.started.clear()
    parser.paused = True
    url = _start(client, job_id)
    assert parser.started.wait(5)
    assert pipeline.pending_work_count() == pipeline.active_count("byok") == 1
    assert pipeline.active_job_ids() == {job_id}
    assert client.get(root).json()["active_expansion"]["status"] == "running"
    for response in (
        client.patch(f"/api/segments/{seg.id}", json={"target": "竞争编辑"}),
        client.post(f"/api/segments/{seg.id}/retranslate", json={}, headers=HEADERS),
        client.post(f"/api/segments/revisions/{revision.id}/revert"),
        client.post(f"{root}/retry", headers=HEADERS),
    ):
        assert response.status_code == 400 and response.json()["detail"] == "正在补翻其余页，完成后即可继续编辑"
    assert client.post(f"{root}/expand-pages", json={}, headers=HEADERS).status_code == 400
    assert client.delete(root).status_code == 400
    assert usecases.purge_expired(pipeline, 24) == 0
    _assert_old_download(client, job_id, old_background)
    assert client.post(f"{url}/cancel").status_code == 202
    assert _wait(client, url)["status"] == "cancelled"
    parser.paused = False
    _assert_old_download(client, job_id, old_background)
    assert client.get(root).json()["active_expansion"] is None


@pytest.mark.parametrize("stage", list(ExpansionStage)[:-1])
@pytest.mark.parametrize("user_cancel", [True, False], ids=["cancel", "restart"])
def test_interruption_at_every_candidate_boundary(app, monkeypatch, stage, user_cancel):
    client, container, pdf, _parser, _chat = app
    job_id = _upload(client, pdf, [0, 1, 2])
    pipeline = container.pipeline
    deps = pipeline.deps
    before = _rows(container, job_id)
    old_background = deps.artifacts.path(job_id, "background.pdf").read_bytes()
    stage_update = expansion._Run.stage

    def interrupt(run, observed):
        stage_update(run, observed)
        if observed is not stage:
            return
        loop = asyncio.get_running_loop()
        if user_cancel:
            loop.call_soon(pipeline.cancel_expansion, run.op)
        else:
            task = asyncio.current_task()
            assert task is not None
            loop.call_soon(task.cancel)

    monkeypatch.setattr(expansion._Run, "stage", interrupt)
    url = _start(client, job_id)
    portal = client.portal
    assert portal is not None
    assert portal.call(pipeline.wait_all, 10)
    if not user_cancel:
        assert client.get(url).json()["status"] == "running"
        pipeline.recover_interrupted()
    op = _wait(client, url)
    assert (op["status"], op["stage"]) == ("cancelled" if user_cancel else "failed", stage.value)
    _assert_old_download(client, job_id, old_background)
    assert _rows(container, job_id) == before


def test_free_channel_ignores_personal_headers_and_checks_paddle_only_when_needed(app, monkeypatch):
    client, container, pdf, parser, _chat = app
    job_id = _upload(client, pdf, [0, 1, 2])
    root = f"/api/jobs/{job_id}"
    deps = container.pipeline.deps
    job = deps.store.get_job(job_id)
    assert job is not None
    job.channel_mode, job.channel_provider_id = "free", "fixture-free"
    job.options.parser = "paddleocr-vl"
    deps.store.save_job(job)
    calls = []

    def free_secrets(self, provider_id=None, *, require_parser, parser):
        calls.append((provider_id, require_parser, parser))
        return Secrets("", "fixture-free-key", "deepseek-flash", "", paddle_token="fixture-paddle", channel_mode="free")

    monkeypatch.setattr(Container, "free_secrets", free_secrets)
    personal = {"X-LLM-Protocol": "invalid", "X-Paddle-Key": "fixture-personal"}
    response = client.post(f"{root}/expand-pages", json={"pages": [3]}, headers=personal)
    assert response.status_code == 400 and "PaddleOCR" in response.json()["detail"]
    deps.artifacts.path(job_id, "layout.json").write_text(parser.layout.model_dump_json(), encoding="utf-8")
    assert _wait(client, _start(client, job_id, [3], personal))["status"] == "done"
    assert calls == [("fixture-free", True, "paddleocr-vl"), ("fixture-free", False, None)]


def test_restart_reaps_only_unpublished_artifacts_and_settles_once(app):
    client, container, pdf, _parser, _chat = app
    job_id = _upload(client, pdf, [0, 1, 2])
    deps = container.pipeline.deps
    before = deps.store.get_job(job_id)
    assert before is not None
    root_background = deps.artifacts.path(job_id, "background.pdf").read_bytes()
    op = ExpansionOp(
        id=uuid4().hex,
        job_id=job_id,
        base_version=1,
        target_version=2,
        requested_pages=[3],
        merged_pages=[0, 1, 2, 3],
        status=ExpansionStatus.RUNNING,
        stage=ExpansionStage.PUBLISH,
        cost_cny=0.125,
    )
    deps.store.create_expansion(op)
    work = candidate_dir(deps.artifacts, job_id, op.id)
    work.mkdir(parents=True)
    unpublished = version_artifact(deps.artifacts, job_id, 2, "ir.json")
    unpublished.parent.mkdir(parents=True)
    unpublished.write_bytes(b"candidate")
    restarted, _, _, _ = make_fixture(container.settings.data_dir)
    try:
        restarted.pipeline.recover_interrupted()
        restarted.pipeline.recover_interrupted()
        store = restarted.pipeline.deps.store
        parent = store.get_job(job_id)
        terminal = store.expansion(job_id, op.id)
        assert terminal and terminal.status is ExpansionStatus.FAILED and terminal.cost_applied
        assert parent and parent.cost_cny == pytest.approx((before.cost_cny or 0) + 0.125)
        assert not work.exists() and not unpublished.exists()
        _assert_old_download(client, job_id, root_background)
    finally:
        restarted.close()


def test_expansion_credentials_page_limit_and_version_grace(app, monkeypatch):
    client, container, pdf, parser, _chat = app
    job_id = _upload(client, pdf, [0, 1, 2])
    pipeline = container.pipeline
    deps = pipeline.deps
    job = deps.store.get_job(job_id)
    assert job is not None
    root = f"/api/jobs/{job_id}"
    path = deps.artifacts.path(job_id, "layout.json")
    wrong = HEADERS | {"X-Parser-Base-Url": "https://wrong.example"}
    assert client.post(f"{root}/expand-pages", json={"pages": [3]}, headers=wrong).status_code == 400
    path.write_text(parser.layout.model_dump_json(), encoding="utf-8")
    # 已有版面时不要求解析器绑定，也不调用解析器。
    result = _wait(client, _start(client, job_id, [3], wrong))
    assert result["status"] == "done", result
    assert parser.calls == [[0, 1, 2]]
    now = datetime.now(UTC)
    usecases.purge_expired(pipeline, 0, now=now + timedelta(seconds=1))
    assert path.is_file()
    usecases.purge_expired(pipeline, 0, now=now + timedelta(seconds=901))
    assert not path.exists()
    assert deps.artifacts.path(job_id, "original.pdf").exists()
    assert deps.artifacts.path(job_id, "glossary.json").exists()
    assert version_artifact(deps.artifacts, job_id, 2, "ir.json").exists()

    job = deps.store.get_job(job_id)
    assert job is not None
    job.channel_mode = "free"
    deps.store.save_job(job)
    build_limits = usecases.Limits(max_upload_mb=20, max_pages=100, free_max_pages=PAGE_COUNT - 1)
    monkeypatch.setattr("pdftr.api.routes_expansions._build_limits", lambda *args: build_limits)
    response = client.post(f"{root}/expand-pages", json={"pages": [4]}, headers=wrong)
    assert response.status_code == 400 and str(PAGE_COUNT - 1) in response.json()["detail"]
