from pathlib import Path

from pdftr.storage.cache import SqliteTranslationCache


def test_cache_roundtrip(tmp_path: Path):
    db = tmp_path / "app.db"
    cache = SqliteTranslationCache(db)
    assert cache.get("k") is None
    cache.put("k", "译文")
    cache.put("k", "新译文")
    cache.close()
    assert SqliteTranslationCache(db).get("k") == "新译文"  # 重开后仍在


def test_purge_older_than(tmp_path):
    cache = SqliteTranslationCache(tmp_path / "app.db")
    cache.put("new", "新")
    with cache._db:
        cache._db.execute(
            "INSERT INTO translation_cache (key, target, created_at) VALUES ('old', '旧', '2020-01-01 00:00:00')"
        )
    assert cache.purge_older_than(7) == 1
    assert cache.get("old") is None and cache.get("new") == "新"


def test_busy_timeout_and_retry_on_real_sqlite_lock(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing

    from pdftr.storage.sqlite import SqliteStore

    path = tmp_path / "app.db"
    cache = SqliteTranslationCache(path)
    store = SqliteStore(path)
    try:
        assert cache._db.execute("PRAGMA busy_timeout").fetchone() == (15000,)
        assert store._db.execute("PRAGMA busy_timeout").fetchone() == (15000,)
        cache._db.execute("PRAGMA busy_timeout=0")
        with closing(sqlite3.connect(path)) as other:
            other.execute("BEGIN IMMEDIATE")
            retries = []

            def unlock(delay):
                retries.append(delay)
                other.rollback()

            from pdftr.storage import connection

            monkeypatch.setattr(connection, "sleep", unlock)
            cache.put("synthetic", "译文")
            assert retries == [0.025]
            assert cache.get("synthetic") == "译文"
    finally:
        store.close()
        cache.close()
