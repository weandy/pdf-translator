import sqlite3
from pathlib import Path

import pytest

from pdftr.storage.metadata_backup import export_metadata


def test_fresh_metadata_backup_excludes_content_sessions_and_old_pages(tmp_path: Path):
    source, target = tmp_path / "app.db", tmp_path / "metadata.db"
    marker = "SYNTHETIC_PRIVATE_CONTENT_SHOULD_NOT_BE_BACKED_UP"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE analytics_daily (day TEXT PRIMARY KEY, page_views INTEGER)")
        db.execute("INSERT INTO analytics_daily VALUES ('2026-11-11', 12)")
        db.execute("CREATE TABLE submission_audit (job_id TEXT PRIMARY KEY, title TEXT, future_body TEXT)")
        db.execute("INSERT INTO submission_audit VALUES ('synthetic-id', 'Synthetic title', ?)", (marker,))
        for table in ("jobs", "segments", "revisions", "translation_cache", "admin_sessions"):
            db.execute(f"CREATE TABLE {table} (data TEXT)")
            db.execute(f"INSERT INTO {table} VALUES (?)", (marker,))
        db.commit()
        counts = export_metadata(source, target)
    assert counts == {"analytics_daily": 1, "submission_audit": 1}
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("analytics_daily",),
            ("submission_audit",),
        ]
        assert [column[1] for column in db.execute("PRAGMA table_info(submission_audit)")] == ["job_id", "title"]
        assert db.execute("SELECT page_views FROM analytics_daily").fetchone() == (12,)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert marker.encode() not in target.read_bytes()
    with pytest.raises(FileExistsError):
        export_metadata(source, target)
    with pytest.raises(ValueError):
        export_metadata(source, source)


def test_missing_source_does_not_create_backup(tmp_path: Path):
    with pytest.raises(ValueError):
        export_metadata(tmp_path / "missing.db", tmp_path / "new.db")
    assert not (tmp_path / "new.db").exists()
