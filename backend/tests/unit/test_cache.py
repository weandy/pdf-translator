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
    cache._db.execute(
        "INSERT INTO translation_cache (key, target, created_at) VALUES ('old', '旧', '2020-01-01 00:00:00')"
    )
    assert cache.purge_older_than(7) == 1
    assert cache.get("old") is None and cache.get("new") == "新"
