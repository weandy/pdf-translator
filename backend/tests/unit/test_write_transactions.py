"""验证提交阶段发生锁冲突时，事务重试不会继承上一次写入。"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from pdftr.domain.admin import AdminSession
from pdftr.domain.ir import Revision, SegmentStatus
from pdftr.domain.job import Job, JobStatus
from pdftr.domain.submissions import SubmissionSearch
from pdftr.storage.admin import SqliteAdminSessionStore
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore


class BusyCommit(sqlite3.Connection):
    fail_next_commit = False
    failures = 0
    rollbacks = 0

    def commit(self):
        if self.fail_next_commit:
            assert self.in_transaction
            self.fail_next_commit = False
            self.failures += 1
            error = sqlite3.OperationalError("synthetic commit conflict")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        super().commit()

    def rollback(self):
        self.rollbacks += 1
        super().rollback()


@pytest.fixture
def inject_busy_commit(monkeypatch):
    connect = sqlite3.connect

    def patched_connect(*args, **kwargs):
        return connect(*args, **kwargs, factory=BusyCommit)

    monkeypatch.setattr(sqlite3, "connect", patched_connect)


def test_job_and_revision_writes_retry_failed_commits(tmp_path, inject_busy_commit):
    store = SqliteStore(tmp_path / "app.db", submission_audit_enabled=True)
    db = store._db
    assert isinstance(db, BusyCommit)
    job = Job(id="0000000000000001", filename="synthetic.pdf", page_count=3)
    try:
        db.fail_next_commit = True
        store.create_job(job)
        assert db.failures == db.rollbacks == 1 and not db.in_transaction

        db.fail_next_commit = True
        job.status = JobStatus.DONE
        store.save_job(job)
        assert db.failures == db.rollbacks == 2 and not db.in_transaction

        db.fail_next_commit = True
        revision = store.add_revision(
            Revision(
                job_id=job.id,
                segment_id="s1",
                action="edit",
                before="old",
                before_status=SegmentStatus.DONE,
                after="new",
                after_status=SegmentStatus.EDITED,
            )
        )
        assert db.failures == db.rollbacks == 3 and not db.in_transaction
        assert store.revisions(job.id) == [revision]
        assert store.all_jobs() == [job]
        assert store.search_submissions(SubmissionSearch()).total == 1
        assert db.execute("SELECT jobs_submitted, jobs_completed, pages_submitted FROM analytics_daily").fetchall() == [
            (1, 1, 3)
        ]
    finally:
        store.close()


def test_cache_and_admin_retry_failed_commits_on_shared_database(tmp_path, inject_busy_commit):
    path = tmp_path / "app.db"
    cache = SqliteTranslationCache(path)
    admin = SqliteAdminSessionStore(path)
    now = datetime.now(UTC)
    session = AdminSession("synthetic-digest", now, now + timedelta(hours=1), "synthetic-version")
    try:
        db = cache._db
        assert isinstance(db, BusyCommit)
        db.fail_next_commit = True
        cache.put("synthetic-key", "synthetic-value")
        assert db.failures == db.rollbacks == 1 and not db.in_transaction
        assert cache.get("synthetic-key") == "synthetic-value"

        db = admin._db
        assert isinstance(db, BusyCommit)
        db.fail_next_commit = True
        admin.save(session)
        assert db.failures == db.rollbacks == 1 and not db.in_transaction
        assert admin.get(session.token_hash) == session
        db.fail_next_commit = True
        admin.purge(session.expires_at, session.password_version)
        assert db.failures == db.rollbacks == 2 and not db.in_transaction
        assert admin.get(session.token_hash) is None
    finally:
        admin.close()
        cache.close()
