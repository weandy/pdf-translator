"""独立版面库的容量、删除与损坏处理。"""

import sqlite3
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.layout import LayoutResult
from pdftr.storage.layout_cache import SqliteLayoutCache


@pytest.fixture()
def layout():
    return LayoutResult(parser="mineru", parser_version="synthetic-private-body", pages=[])


def counts(path):
    with closing(sqlite3.connect(path)) as db:
        return (
            db.execute("SELECT count(*) FROM layout_cache").fetchone()[0],
            db.execute("SELECT count(*) FROM layout_cache_refs").fetchone()[0],
        )


def test_references_overwrite_released_cells_and_checkpoint(tmp_path, layout):
    path = tmp_path / "layout_cache.db"
    with closing(SqliteLayoutCache(path)) as cache:
        assert cache.put("key", layout, job_id="first", ttl_seconds=24, now_ts=10)
        assert cache.get("key", job_id="second", now_ts=11) == layout
        assert counts(path) == (1, 2)
        assert cache.release_job("first") == 0
        assert counts(path) == (1, 1)
        blob = zlib.compress(layout.model_dump_json().encode())
        cache._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert blob in path.read_bytes()
        assert cache.release_job("second") == 1
        assert counts(path) == (0, 0)
        assert blob not in path.read_bytes()
        assert path.with_suffix(".db-wal").stat().st_size == 0
        assert cache._db.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_concurrent_same_key_never_extends_original_ttl(tmp_path, layout):
    path = tmp_path / "layout_cache.db"
    with closing(SqliteLayoutCache(path)) as cache:
        cache.put("key", layout, job_id="first", ttl_seconds=10, now_ts=10)
        with ThreadPoolExecutor(max_workers=2) as executor:
            second = executor.submit(cache.put, "key", layout, job_id="second", ttl_seconds=100, now_ts=11)
            third = executor.submit(cache.get, "key", job_id="third", now_ts=11)
            assert second.result(timeout=2)
            assert third.result(timeout=2) == layout
        assert counts(path) == (1, 3)
        assert cache.get("key", job_id="fourth", now_ts=20) is None
        assert counts(path) == (0, 0)


@pytest.mark.parametrize("damage", [b"broken", zlib.compress(b"a" * 2048)])
def test_corrupt_and_oversized_decompression_are_self_healing(tmp_path, layout, damage):
    path = tmp_path / "layout_cache.db"
    with closing(SqliteLayoutCache(path, max_uncompressed_bytes=1024)) as cache:
        cache.put("key", layout, job_id="job", ttl_seconds=100, now_ts=10)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE layout_cache SET layout_blob = ?", (damage,))
        assert cache.get("key", job_id="reader", now_ts=11) is None
        assert counts(path) == (0, 0)
        assert cache.put("key", layout, job_id="reader", ttl_seconds=100, now_ts=12)
        assert cache.get("key", job_id="reader", now_ts=13) == layout


@pytest.mark.parametrize("by_count", [True, False])
def test_cache_evicts_oldest_when_capacity_exceeded(tmp_path, layout, by_count):
    path = tmp_path / "layout_cache.db"
    compressed_size = len(zlib.compress(layout.model_dump_json().encode()))
    limits = {"max_entries": 2} if by_count else {"max_total_bytes": compressed_size * 2}
    with closing(SqliteLayoutCache(path, **limits)) as cache:
        for i in range(3):
            assert cache.put(str(i), layout, job_id=str(i), ttl_seconds=100, now_ts=10 + i)
        assert counts(path) == (2, 2)
        assert cache.get("0", job_id="reader", now_ts=20) is None
        assert cache.get("1", job_id="reader", now_ts=20) == layout


@pytest.mark.parametrize("limits", [{"max_entry_bytes": 1}, {"max_uncompressed_bytes": 1}, {"max_total_bytes": 1}])
def test_oversized_entry_never_written(tmp_path, layout, limits):
    path = tmp_path / "layout_cache.db"
    with closing(SqliteLayoutCache(path, **limits)) as cache:
        assert not cache.put("key", layout, job_id="job", ttl_seconds=100)
        assert counts(path) == (0, 0)


def test_purge_expired_and_orphan_references(tmp_path, layout):
    path = tmp_path / "layout_cache.db"
    with closing(SqliteLayoutCache(path)) as cache:
        for key, ttl in [("active", 100), ("orphan", 100), ("expired", 1)]:
            assert cache.put(key, layout, job_id=key, ttl_seconds=ttl, now_ts=10)
        assert cache.purge_expired(known_job_ids={"active", "expired"}, now_ts=12) == 2
        assert counts(path) == (1, 1)
        assert cache.clear_all() == 1
        assert counts(path) == (0, 0)


@pytest.mark.parametrize("disabled", [{"retention_hours": 0}, {"layout_cache_ttl_hours": 0}])
def test_disabled_startup_clears_old_layout_and_keeps_main_db_separate(tmp_path, layout, disabled):
    path = tmp_path / "layout_cache.db"
    with closing(SqliteLayoutCache(path)) as old:
        old.put("key", layout, job_id="job", ttl_seconds=100)
    container = build_container(Settings(data_dir=tmp_path, **disabled))
    cache = container.pipeline.deps.layout_cache
    assert cache is not None
    with closing(cache), closing(sqlite3.connect(tmp_path / "app.db")) as db:
        assert container.pipeline.deps.layout_cache_ttl_hours == 0
        assert counts(path) == (0, 0)
        assert db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'layout_cache%'").fetchall() == []


def test_layout_cache_retries_external_sqlite_lock(tmp_path, layout, monkeypatch):
    from pdftr.storage import connection

    path = tmp_path / "layout_cache.db"
    with closing(SqliteLayoutCache(path)) as cache, closing(sqlite3.connect(path)) as other:
        cache._db.execute("PRAGMA busy_timeout=0")
        other.execute("BEGIN IMMEDIATE")
        retries = []

        def unlock(delay):
            retries.append(delay)
            other.rollback()

        monkeypatch.setattr(connection, "sleep", unlock)
        assert cache.put("key", layout, job_id="job", ttl_seconds=60)
        assert len(retries) == 1
        assert counts(path) == (1, 1)
