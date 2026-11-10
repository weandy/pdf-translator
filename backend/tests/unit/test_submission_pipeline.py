"""提交接收、IR 断点与临时文件清理的集成边界，完全离线。"""

import asyncio
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pdftr.domain.ir import DocMeta, Document
from pdftr.domain.job import Job, JobOptions, JobStatus
from pdftr.domain.layout import LayoutResult
from pdftr.domain.submissions import SubmissionContext, SubmissionSearch
from pdftr.fusion.build import FusionReport
from pdftr.pipeline import runner, usecases
from pdftr.pipeline.events import EventBus
from pdftr.pipeline.runner import Pipeline, PipelineDeps
from pdftr.pipeline.secrets import Secrets, SecretStore
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.files import FileArtifactStore
from pdftr.storage.sqlite import SqliteStore

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
SECRETS = Secrets("https://example.invalid", "synthetic-key", "synthetic-model", "synthetic-token")
CONTEXT = SubmissionContext(submitter_ip="2001:db8::8", ip_source="direct", notice_version="synthetic-v1")


def unexpected():
    raise AssertionError("测试不能调用外部服务")


@pytest.fixture
def environment(tmp_path: Path):
    store = SqliteStore(tmp_path / "app.db", submission_audit_enabled=True)
    cache = SqliteTranslationCache(tmp_path / "app.db")
    artifacts = FileArtifactStore(tmp_path)
    pipeline = Pipeline(
        PipelineDeps(
            store=store,
            cache=cache,
            artifacts=artifacts,
            events=EventBus(),
            secrets=SecretStore(),
            parser_factory=lambda sec, name: unexpected(),
            chat_factory=lambda sec: unexpected(),
            term_matcher=lambda: None,
        )
    )
    yield pipeline, store, artifacts
    assert isinstance(pipeline.deps.store, SqliteStore)
    pipeline.deps.store.close()
    cache.close()


def receive(pipeline: Pipeline, tmp_path: Path, **values) -> Job:
    return usecases.create_job(
        pipeline,
        b"%PDF-synthetic",
        "synthetic.pdf",
        JobOptions(cloud_consent=True),
        SECRETS,
        usecases.Limits(max_upload_mb=2, max_pages=10, data_dir=tmp_path),
        CONTEXT,
        **values,
    )


def test_rejected_upload_never_creates_task_or_ledger(environment, tmp_path: Path):
    pipeline, store, _ = environment
    with pytest.raises(usecases.UserError):
        usecases.create_job(
            pipeline,
            b"not a PDF",
            "synthetic.pdf",
            JobOptions(cloud_consent=True),
            SECRETS,
            usecases.Limits(max_upload_mb=2, max_pages=10, data_dir=tmp_path),
            CONTEXT,
        )
    assert not store.all_jobs() and store.search_submissions(SubmissionSearch()).total == 0


def test_database_failure_compensates_files_before_dispatch(environment, tmp_path: Path, monkeypatch):
    pipeline, store, artifacts = environment
    monkeypatch.setattr(usecases, "page_count", lambda _: 2)
    dispatched = []
    monkeypatch.setattr(pipeline, "submit", lambda *args: dispatched.append(True))

    def fail(*args, **kwargs):
        raise sqlite3.IntegrityError("synthetic rollback")

    monkeypatch.setattr(store._submissions, "create", fail)
    with pytest.raises(sqlite3.IntegrityError):
        receive(pipeline, tmp_path)
    assert not store.all_jobs() and not dispatched
    assert not list(artifacts.root.iterdir())
    assert store._db.execute("SELECT COUNT(*) FROM analytics_jobs").fetchone()[0] == 0


def test_failed_compensation_is_persisted_for_retry(environment, tmp_path: Path, monkeypatch):
    pipeline, store, artifacts = environment
    monkeypatch.setattr(usecases, "page_count", lambda _: 2)

    def fail(*args, **kwargs):
        raise OSError("synthetic failure")

    monkeypatch.setattr(store._submissions, "create", fail)
    monkeypatch.setattr(artifacts, "remove", fail)
    with pytest.raises(OSError):
        receive(pipeline, tmp_path)
    pending = store.pending_artifact_cleanup()
    assert len(pending) == 1 and store.get_job(pending[0]) is None
    assert (artifacts.root / pending[0] / "original.pdf").exists()


def test_dispatch_failure_returns_received_retryable_task(environment, tmp_path: Path, monkeypatch):
    pipeline, store, _ = environment
    monkeypatch.setattr(usecases, "page_count", lambda _: 2)

    def fail(*args):
        raise RuntimeError("synthetic scheduler failure")

    monkeypatch.setattr(pipeline, "submit", fail)
    current = receive(pipeline, tmp_path)
    assert current.status is JobStatus.FAILED and "重试" in current.error
    assert store.get_job(current.id).status is JobStatus.FAILED
    record = store.search_submissions(SubmissionSearch()).items[0]
    assert record.status is JobStatus.FAILED and record.submitter_ip == CONTEXT.submitter_ip


@pytest.mark.parametrize("existing_ir", [False, True])
def test_fuse_updates_title_on_both_new_and_resume_paths(environment, monkeypatch, existing_ir):
    pipeline, store, artifacts = environment
    current = Job(id="0000000000000001", filename="synthetic.pdf", page_count=2)
    store.create_job(current, CONTEXT)
    doc = Document(job_id=current.id, pages=[], meta=DocMeta(title="A Synthetic Parsed Title"))
    if existing_ir:
        artifacts.path(current.id, "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
    else:
        artifacts.path(current.id, "layout.json").write_text(
            LayoutResult(parser="synthetic", pages=[]).model_dump_json(), encoding="utf-8"
        )
        monkeypatch.setattr(runner, "extract", lambda _: [])
        monkeypatch.setattr(runner, "fuse", lambda *args, **kwargs: (doc, FusionReport(pages=2)))
    asyncio.run(pipeline._fuse(current, SECRETS))
    record = store.search_submissions(SubmissionSearch()).items[0]
    assert record.title == "A Synthetic Parsed Title" and record.title_source == "parsed"
    assert record.submitter_ip == CONTEXT.submitter_ip and record.page_count == 2


def test_delete_failure_survives_restart_and_retry_preserves_audit(environment, tmp_path: Path, monkeypatch):
    pipeline, store, artifacts = environment
    current = Job(id="0000000000000001", filename="synthetic.pdf")
    store.create_job(current, CONTEXT)
    artifacts.path(current.id, "original.pdf").write_bytes(b"synthetic temporary content")
    real_remove = artifacts.remove

    def fail(_):
        raise PermissionError("synthetic locked file")

    monkeypatch.setattr(artifacts, "remove", fail)
    usecases.delete_job(pipeline, current.id)
    assert store.get_job(current.id) is None and store.pending_artifact_cleanup() == [current.id]
    assert store.search_submissions(SubmissionSearch()).total == 1
    store.close()
    reopened = SqliteStore(tmp_path / "app.db")
    pipeline.deps = replace(pipeline.deps, store=reopened)
    monkeypatch.setattr(artifacts, "remove", real_remove)
    assert usecases.purge_expired(pipeline, 0, NOW) == 0
    assert not reopened.pending_artifact_cleanup() and not (artifacts.root / current.id).exists()
    assert reopened.search_submissions(SubmissionSearch()).total == 1


def test_orphan_scan_protects_running_known_fresh_and_non_job_directories(environment, monkeypatch):
    pipeline, store, artifacts = environment
    ids = [f"{index:016x}" for index in range(1, 5)]
    old_orphan, active_orphan, fresh_orphan, known = ids
    for job_id in ids:
        folder = artifacts.job_dir(job_id)
        artifacts.path(job_id, "original.pdf").write_bytes(b"synthetic")
        timestamp = (NOW - timedelta(hours=48)).timestamp() if job_id != fresh_orphan else NOW.timestamp()
        os.utime(folder, (timestamp, timestamp))
    (artifacts.root / "unrelated").mkdir()
    store.create_job(Job(id=known, filename="synthetic.pdf", expires_at=NOW + timedelta(hours=1)), CONTEXT)
    monkeypatch.setattr(pipeline, "is_running", lambda job_id: job_id == active_orphan)
    assert usecases.purge_expired(pipeline, 24, NOW) == 0
    assert not (artifacts.root / old_orphan).exists()
    assert all((artifacts.root / job_id).exists() for job_id in (active_orphan, fresh_orphan, known))
    assert (artifacts.root / "unrelated").exists()


def test_expired_running_task_and_ledger_remain_then_cleanup_after_finish(environment, monkeypatch):
    pipeline, store, artifacts = environment
    current = Job(id="0000000000000001", filename="synthetic.pdf", expires_at=NOW - timedelta(hours=1))
    store.create_job(current, CONTEXT)
    artifacts.path(current.id, "original.pdf").write_bytes(b"synthetic")
    monkeypatch.setattr(pipeline, "is_running", lambda _: True)
    assert usecases.purge_expired(pipeline, 24, NOW) == 0
    assert store.get_job(current.id) is not None
    monkeypatch.setattr(pipeline, "is_running", lambda _: False)
    assert usecases.purge_expired(pipeline, 24, NOW) == 1
    assert store.get_job(current.id) is None and not (artifacts.root / current.id).exists()
    assert store.search_submissions(SubmissionSearch()).total == 1


def test_remove_validates_workspace_and_does_not_create_missing_dir(tmp_path: Path):
    artifacts = FileArtifactStore(tmp_path)
    artifacts.remove("0000000000000001")
    assert not artifacts.root.exists()
    with pytest.raises(ValueError):
        artifacts.remove("../outside")
    outside = tmp_path / "outside"
    outside.mkdir()
    artifacts.root.mkdir()
    link = artifacts.root / "0000000000000002"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        return  # Windows 没有符号链接权限时，路径穿越验证仍已执行。
    with pytest.raises(ValueError):
        artifacts.remove(link.name)
    assert outside.exists()
