"""统计幂等、跨日、重启和旧数据库升级；仅使用合成任务与随机访客标识。"""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from pdftr.domain.job import Job, JobStatus
from pdftr.domain.stats import StatsCounts
from pdftr.storage import sqlite as sqlite_module
from pdftr.storage.sqlite import SqliteStore

FIRST = datetime(2026, 11, 7, 15, 59, tzinfo=UTC)


def set_now(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    monkeypatch.setattr(sqlite_module, "utc_now", lambda: now)


def test_empty_summary_has_zero_filled_local_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    store = SqliteStore(tmp_path / "app.db")
    result = store.stats()
    assert result.since == FIRST
    assert result.generated_at == FIRST
    assert result.timezone == "Asia/Shanghai"
    assert result.period.start_date == "2026-11-01"
    assert result.period.end_date == "2026-11-07"
    assert [row.date for row in result.daily] == [f"2026-11-{day:02}" for day in range(1, 8)]
    assert [row.available for row in result.daily] == [False] * 6 + [True]
    assert not any(result.period.counts.model_dump().values())
    assert not any(result.today.model_dump().values())
    assert "total" not in result.model_dump()
    store.close()


def test_job_counts_survive_retry_delete_and_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    path = tmp_path / "app.db"
    store = SqliteStore(path)
    job = Job(id="0000000000000001", filename="synthetic.pdf", page_count=4)
    for status in [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED, JobStatus.RUNNING, JobStatus.DONE]:
        job.status = status
        store.save_job(job)
    store.save_job(job)
    store.delete_job(job.id)
    assert store.get_job(job.id) is None
    store.close()
    reopened = SqliteStore(path)
    # 即使同一随机任务 ID 再次出现，也不能将一次使用计为多次。
    reopened.save_job(job)
    result = reopened.stats()
    assert result.period.counts.jobs_submitted == result.period.counts.jobs_completed == 1
    assert result.period.counts.pages_submitted == 4
    assert result.since == FIRST
    reopened.close()


def test_submission_and_completion_belong_to_their_local_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    store = SqliteStore(tmp_path / "app.db")
    job = Job(id="0000000000000002", filename="synthetic.pdf", page_count=2)
    store.save_job(job)
    set_now(monkeypatch, FIRST + timedelta(minutes=2))
    job.status = JobStatus.DONE
    store.save_job(job)
    result = store.stats()
    assert result.generated_at == FIRST + timedelta(minutes=2)
    assert result.period.start_date == "2026-11-02"
    assert result.period.end_date == "2026-11-08"
    assert [row.date for row in result.daily[-2:]] == ["2026-11-07", "2026-11-08"]
    assert [row.available for row in result.daily] == [False] * 5 + [True, True]
    assert result.daily[-2].jobs_submitted == 1
    assert result.daily[-2].jobs_completed == 0
    assert result.daily[-2].pages_submitted == 2
    assert result.today.jobs_submitted == 0
    assert result.today.jobs_completed == 1
    store.close()


def test_visits_deduplicate_events_and_anonymous_browsers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    store = SqliteStore(tmp_path / "app.db")
    browser, other = str(uuid4()), str(uuid4())
    event = str(uuid4())
    store.record_visit(event, browser)
    store.record_visit(event, browser)
    store.record_visit(event, other)
    store.record_visit(str(uuid4()), browser)
    result = store.stats()
    assert result.period.counts.page_views == 2
    assert result.period.counts.visitors == result.today.visitors == 1
    set_now(monkeypatch, FIRST + timedelta(minutes=2))
    store.record_visit(str(uuid4()), browser)
    result = store.stats()
    assert result.period.counts.page_views == 3
    assert result.period.counts.visitors == 1
    assert [row.visitors for row in result.daily[-2:]] == [1, 1]
    hashes = store._db.execute("SELECT visitor_hash FROM analytics_visitors").fetchall()
    assert hashes == [(hashlib.sha256(browser.encode()).hexdigest(),)]
    assert browser not in repr(hashes)
    store.close()


def test_window_advances_without_new_visits_and_cleanup_keeps_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    store = SqliteStore(tmp_path / "app.db")
    browser = str(uuid4())
    old_event = str(uuid4())
    store.record_visit(old_event, browser)
    set_now(monkeypatch, FIRST + timedelta(days=9))
    result = store.stats()
    assert result.generated_at == FIRST + timedelta(days=9)
    assert result.period.start_date == "2026-11-10"
    assert result.period.end_date == "2026-11-16"
    assert all(row.available for row in result.daily)
    assert not any(result.period.counts.model_dump().values())
    # 即便没有触发明细清理，过期访问也不能进入当前公开窗口。
    assert store._db.execute("SELECT COUNT(*) FROM analytics_visit_days").fetchone()[0] == 1
    store.record_visit(str(uuid4()), browser)
    result = store.stats()
    assert result.period.counts.page_views == result.period.counts.visitors == 1
    assert result.daily[-1].page_views == 1
    assert store._db.execute("SELECT SUM(page_views) FROM analytics_daily").fetchone()[0] == 2
    assert (
        store._db.execute("SELECT COUNT(*) FROM analytics_events WHERE event_id = ?", (old_event,)).fetchone()[0] == 0
    )
    assert store._db.execute("SELECT COUNT(*) FROM analytics_visit_days").fetchone()[0] == 1
    store.close()


def test_visit_deduplication_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    path = tmp_path / "app.db"
    event, browser = str(uuid4()), str(uuid4())
    store = SqliteStore(path)
    store.record_visit(event, browser)
    store.close()
    reopened = SqliteStore(path)
    reopened.record_visit(event, browser)
    reopened.record_visit(str(uuid4()), browser)
    result = reopened.stats()
    assert result.period.counts.page_views == 2
    assert result.period.counts.visitors == result.today.visitors == 1
    assert result.since == FIRST
    reopened.close()


def test_existing_database_is_not_reported_as_new_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    path = tmp_path / "app.db"
    old = Job(id="0000000000000003", filename="synthetic-existing.pdf", page_count=7)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, data TEXT, created_at TEXT)")
        db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?)", (old.id, old.status, old.model_dump_json(), "2026-11-01"))
    store = SqliteStore(path)
    assert store.get_job(old.id) == old
    assert store.stats().period.counts.jobs_submitted == 0
    old.status = JobStatus.DONE
    store.save_job(old)
    assert store.stats().period.counts.jobs_completed == 0
    store.save_job(Job(id="0000000000000004", filename="synthetic-new.pdf", page_count=3))
    assert store.stats().period.counts.jobs_submitted == 1
    assert store.stats().period.counts.pages_submitted == 3
    store.close()


def test_concurrent_connections_cannot_double_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    path = tmp_path / "app.db"
    stores = [SqliteStore(path), SqliteStore(path)]
    event, browser = str(uuid4()), str(uuid4())
    job = Job(id="0000000000000005", filename="synthetic.pdf", page_count=2, status=JobStatus.DONE)

    def write(index: int):
        store = stores[index % 2]
        store.record_visit(event, browser)
        store.save_job(job.model_copy())

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(30)))
    result = stores[0].stats()
    assert result.period.counts.page_views == result.period.counts.visitors == 1
    assert result.period.counts.jobs_submitted == result.period.counts.jobs_completed == 1
    assert result.period.counts.pages_submitted == 2
    for store in stores:
        store.close()


def test_failed_job_transaction_cannot_partially_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    store = SqliteStore(tmp_path / "app.db")
    original = store._stats.job_saved

    def broken(job: Job, now: datetime) -> None:
        original(job, now)
        raise RuntimeError("模拟提交前中断")

    monkeypatch.setattr(store._stats, "job_saved", broken)
    job = Job(id="0000000000000006", filename="synthetic.pdf", page_count=1)
    with pytest.raises(RuntimeError, match="模拟"):
        store.save_job(job)
    assert store.get_job(job.id) is None
    assert store.stats().period.counts.jobs_submitted == 0
    monkeypatch.setattr(store._stats, "job_saved", original)
    store.save_job(job)
    assert store.stats().period.counts.jobs_submitted == 1
    store.close()


def test_period_deduplicates_browsers_across_seven_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST - timedelta(days=7))
    store = SqliteStore(tmp_path / "app.db")
    browser, other, outside = str(uuid4()), str(uuid4()), str(uuid4())
    store.record_visit(str(uuid4()), outside)
    store.save_job(Job(id="0000000000000008", filename="synthetic-old.pdf", page_count=20, status=JobStatus.DONE))
    for offset in range(6, -1, -1):
        set_now(monkeypatch, FIRST - timedelta(days=offset))
        store.record_visit(str(uuid4()), browser)
    result = store.stats()
    assert [row.visitors for row in result.daily] == [1] * 7
    assert result.period.counts.visitors == 1
    assert result.period.counts.page_views == 7
    assert result.period.counts.jobs_submitted == result.period.counts.jobs_completed == 0
    assert result.period.counts.pages_submitted == 0
    assert all(row.available for row in result.daily)
    # 清理余量仍保存第八个日期，但窗口查询必须排除它。
    assert store._db.execute("SELECT COUNT(DISTINCT day) FROM analytics_visit_days").fetchone()[0] == 8
    store.record_visit(str(uuid4()), other)
    result = store.stats()
    assert result.period.counts.visitors == result.today.visitors == 2
    assert result.period.counts.page_views == 8
    assert [row.visitors for row in result.daily] == [1] * 6 + [2]
    store.close()


def test_period_excludes_future_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    store = SqliteStore(tmp_path / "app.db")
    store.record_visit(str(uuid4()), str(uuid4()))
    with store._db:
        store._db.execute("INSERT INTO analytics_daily (day, page_views, visitors) VALUES ('2026-11-08', 3, 1)")
        store._db.execute("INSERT INTO analytics_visit_days (day, visitor_hash) VALUES ('2026-11-08', 'synthetic')")
    result = store.stats()
    assert result.period.counts.page_views == result.period.counts.visitors == 1
    store.close()


def test_summary_uses_one_snapshot_during_another_connections_visit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_now(monkeypatch, FIRST)
    path = tmp_path / "app.db"
    reader, writer = SqliteStore(path), SqliteStore(path)
    written = []

    def write_between_queries(statement: str) -> None:
        if "SELECT COUNT(DISTINCT visitor_hash)" in statement and not written:
            writer.record_visit(str(uuid4()), str(uuid4()))
            written.append(True)

    # 在每日值与跨日去重查询之间提交访问，两个查询仍须读取同一个快照。
    reader._db.set_trace_callback(write_between_queries)
    result = reader.stats(7)
    reader._db.set_trace_callback(None)
    assert written == [True]
    assert result.period.counts.page_views == result.period.counts.visitors == 0
    assert result.today.page_views == result.today.visitors == 0
    assert all(row.page_views == row.visitors == 0 for row in result.daily)
    refreshed = reader.stats(7)
    assert refreshed.period.counts.page_views == refreshed.period.counts.visitors == 1
    reader.close()
    writer.close()


@pytest.mark.parametrize("days", [0, 1, 6, 8, 30, 90])
def test_storage_rejects_non_public_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, days: int):
    set_now(monkeypatch, FIRST)
    store = SqliteStore(tmp_path / "app.db")
    with pytest.raises(ValueError, match="7 天"):
        store.stats(days)
    store.close()


@pytest.mark.parametrize("field", list(StatsCounts.model_fields))
def test_counts_reject_negative_values(field: str):
    with pytest.raises(ValueError):
        StatsCounts(**{field: -1})
