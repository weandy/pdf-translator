"""只用合成元数据验证账本的事务、停采边界和受限查询。"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdftr.domain.job import Job, JobStatus
from pdftr.domain.submissions import SubmissionContext, SubmissionSearch
from pdftr.storage.sqlite import SqliteStore

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
CONTEXT = SubmissionContext(submitter_ip="::ffff:192.0.2.8", ip_source="trusted_proxy", notice_version="synthetic-v1")


@pytest.fixture
def store(tmp_path: Path):
    result = SqliteStore(tmp_path / "app.db", submission_audit_enabled=True)
    yield result
    result.close()


def job(index: int = 1, **values) -> Job:
    return Job(id=f"{index:016x}", filename="synthetic.pdf", page_count=3, created_at=NOW, **values)


def test_creation_is_atomic_with_stats_and_audit_and_duplicate_does_not_replace(store: SqliteStore):
    store.create_job(job(), CONTEXT)
    first = store.search_submissions(SubmissionSearch()).items[0]
    assert first.title == "synthetic" and first.title_source == "filename"
    assert first.submitter_ip == "192.0.2.8" and first.ip_source == "trusted_proxy"
    assert first.page_count == 3 and first.notice_version == "synthetic-v1"
    with pytest.raises(sqlite3.IntegrityError):
        store.create_job(job(), SubmissionContext())
    assert store.search_submissions(SubmissionSearch()).items == [first]
    store._db.execute(
        "CREATE TRIGGER reject_audit BEFORE INSERT ON submission_audit BEGIN SELECT RAISE(ABORT, 'test'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.create_job(job(2), CONTEXT)
    assert store.get_job(job(2).id) is None
    assert store._db.execute("SELECT COUNT(*) FROM analytics_jobs").fetchone()[0] == 1
    counts = store._db.execute("SELECT SUM(jobs_submitted), SUM(pages_submitted) FROM analytics_daily").fetchone()
    assert counts == (1, 3)


def test_save_does_not_invent_record_and_first_completion_survives_retries(store: SqliteStore, monkeypatch):
    monkeypatch.setattr("pdftr.storage.sqlite.utc_now", lambda: NOW)
    current = job()
    store.create_job(current, CONTEXT)
    current.status = JobStatus.DONE
    store.save_job(current)
    for state in (JobStatus.FAILED, JobStatus.QUEUED, JobStatus.DONE):
        current.status = state
        store.save_job(current)
    store.save_job(job(2, status=JobStatus.DONE))
    record = store.search_submissions(SubmissionSearch()).items[0]
    assert record.first_completed_at == NOW
    assert record.submitted_at == NOW and record.submitter_ip == CONTEXT.submitter_ip
    assert store.search_submissions(SubmissionSearch()).total == 1
    store.delete_job(current.id)
    assert store.search_submissions(SubmissionSearch()).total == 1


def test_disabled_collection_never_updates_or_backfills_completion(tmp_path: Path, monkeypatch):
    path = tmp_path / "app.db"
    current = job()
    enabled = SqliteStore(path, submission_audit_enabled=True)
    enabled.create_job(current, CONTEXT)
    original = enabled.search_submissions(SubmissionSearch()).items[0]
    enabled.close()
    disabled = SqliteStore(path)
    current.status = JobStatus.DONE
    disabled.save_job(current)
    disabled.create_job(job(2), CONTEXT)
    disabled.update_submission_title(current.id, "Ignored Synthetic Title")
    assert disabled.backfill_submissions() == 0
    assert disabled.search_submissions(SubmissionSearch()).items == [original]
    disabled.close()
    reopened = SqliteStore(path, submission_audit_enabled=True)
    try:
        monkeypatch.setattr("pdftr.storage.sqlite.utc_now", lambda: NOW + timedelta(days=2))
        for state in (JobStatus.DONE, JobStatus.FAILED, JobStatus.DONE):
            current.status = state
            reopened.save_job(current)
        reopened.save_job(job(2, status=JobStatus.DONE))
        record = reopened.search_submissions(SubmissionSearch()).items[0]
        assert record.first_completed_at is None
        assert record.title == original.title
        assert reopened.search_submissions(SubmissionSearch()).total == 1
    finally:
        reopened.close()


def test_explicit_backfill_is_idempotent_unknown_and_does_not_recount(store: SqliteStore):
    store.save_job(job(status=JobStatus.DONE))
    before = store._db.execute("SELECT SUM(jobs_submitted), SUM(jobs_completed) FROM analytics_daily").fetchone()
    assert store.backfill_submissions({job().id: "Recovered Synthetic Title"}) == 1
    assert store.backfill_submissions() == 0
    record = store.search_submissions(SubmissionSearch()).items[0]
    assert record.record_source == "backfill" and record.title_source == "parsed"
    assert record.title == "Recovered Synthetic Title"
    assert record.submitter_ip is None and record.ip_source == "unknown" and record.notice_version is None
    assert record.first_completed_at is None
    after = store._db.execute("SELECT SUM(jobs_submitted), SUM(jobs_completed) FROM analytics_daily").fetchone()
    assert after == before


def test_title_sanitation_and_partial_updates_preserve_context(store: SqliteStore):
    current = job()
    current.filename = "C:\\synthetic\\folder/safe\x00 name.pdf"
    store.create_job(current, CONTEXT)
    assert store.search_submissions(SubmissionSearch()).items[0].title == "safe name"
    store.update_submission_title(current.id, "合成\x00\u200b论文\n" + "长" * 600)
    record = store.search_submissions(SubmissionSearch()).items[0]
    assert len(record.title) == 500 and record.title.startswith("合成论文 ")
    assert record.title_source == "parsed" and record.submitter_ip == CONTEXT.submitter_ip
    store.update_submission_title(current.id, "")
    assert store.search_submissions(SubmissionSearch()).items[0] == record


@pytest.mark.parametrize(
    "bad",
    [
        {"ip": "192.0.2.8:80"},
        {"ip": "fe80::1%lo"},
        {"page_size": 20},
        {"page": 0},
        {"date_from": "2026-11-12", "date_to": "2026-11-11"},
        {"title_query": "a" * 501},
    ],
)
def test_restricted_search_rejects_invalid_values(bad):
    with pytest.raises(ValidationError):
        SubmissionSearch.model_validate(bad)


def test_search_escapes_wildcards_normalizes_ip_and_has_stable_pages(store: SqliteStore):
    for index in range(1, 55):
        store.create_job(job(index), CONTEXT)
    store.update_submission_title(job().id, "合成 100%_\\ 标题")
    assert store.search_submissions(SubmissionSearch(title_query="%_\\")).total == 1
    assert store.search_submissions(SubmissionSearch(title_query="合成")).total == 1
    assert store.search_submissions(SubmissionSearch(ip="::ffff:c000:208")).total == 54
    first = store.search_submissions(SubmissionSearch())
    second = store.search_submissions(SubmissionSearch(page=2))
    assert [item.job_id for item in first.items] == [job(index).id for index in range(54, 29, -1)]
    assert [item.job_id for item in second.items] == [job(index).id for index in range(29, 4, -1)]
    assert first.total == second.total == 54
    assert len(store.search_submissions(SubmissionSearch(page_size=50)).items) == 50
    assert len(store.search_submissions(SubmissionSearch(page_size=100)).items) == 54


def test_date_filter_uses_inclusive_beijing_days_and_exclusive_utc_end(store: SqliteStore):
    for index, moment in enumerate(
        (
            "2026-11-10T15:59:59+00:00",
            "2026-11-10T16:00:00+00:00",
            "2026-11-11T15:59:59+00:00",
            "2026-11-11T16:00:00+00:00",
        ),
        start=1,
    ):
        current = job(index)
        current.created_at = datetime.fromisoformat(moment)
        store.create_job(current, CONTEXT)
    result = store.search_submissions(
        SubmissionSearch.model_validate({"date_from": "2026-11-11", "date_to": "2026-11-11"})
    )
    assert [record.job_id for record in result.items] == [job(3).id, job(2).id]


def test_count_and_items_share_snapshot_across_external_connection(store: SqliteStore, tmp_path: Path, monkeypatch):
    store.create_job(job(), CONTEXT)
    writer = SqliteStore(tmp_path / "app.db", submission_audit_enabled=True)
    original = store._submissions.db

    class InterleavingConnection:
        def execute(self, sql, params):
            if sql.startswith("SELECT job_id"):
                writer.create_job(job(2), CONTEXT)
            return original.execute(sql, params)

    monkeypatch.setattr(store._submissions, "db", InterleavingConnection())
    try:
        result = store.search_submissions(SubmissionSearch())
        assert result.total == 1 and [item.job_id for item in result.items] == [job().id]
    finally:
        writer.close()
