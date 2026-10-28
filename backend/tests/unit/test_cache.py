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
