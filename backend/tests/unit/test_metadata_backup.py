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


def test_metadata_backup_excludes_runtime_config_tables_and_ciphertexts(tmp_path: Path):
    source, target = tmp_path / "app.db", tmp_path / "metadata.db"
    ciphertext_current = "v1:SYNTHETIC_CURRENT_CONFIG_CIPHERTEXT"
    ciphertext_revision = "v1:SYNTHETIC_REVISION_CONFIG_CIPHERTEXT"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """
            CREATE TABLE analytics_client_daily (
                day TEXT NOT NULL,
                metric_type TEXT NOT NULL,
                device TEXT NOT NULL,
                os TEXT NOT NULL,
                browser TEXT NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (day, metric_type, device, os, browser)
            )
            """
        )
        db.execute("INSERT INTO analytics_client_daily VALUES ('2026-11-11', 'job', 'desktop', 'Windows', 'Chrome', 5)")
        db.execute(
            """
            CREATE TABLE submission_audit (
                job_id TEXT PRIMARY KEY,
                title TEXT,
                channel_mode TEXT,
                model_name TEXT,
                fallback_used INTEGER,
                user_agent TEXT,
                client_device TEXT,
                client_os TEXT,
                client_browser TEXT
            )
            """
        )
        db.execute(
            "INSERT INTO submission_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-1",
                "Synthetic paper",
                "free",
                "gpt-4o-mini",
                1,
                "Mozilla/5.0 Chrome/131.0",
                "desktop",
                "Windows",
                "Chrome",
            ),
        )
        db.execute(
            "CREATE TABLE runtime_config (id INTEGER PRIMARY KEY, payload_enc TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute("INSERT INTO runtime_config VALUES (1, ?, '2026-11-11T00:00:00Z')", (ciphertext_current,))
        db.execute(
            "CREATE TABLE runtime_config_revisions ("
            "id INTEGER PRIMARY KEY, payload_enc TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO runtime_config_revisions VALUES (1, ?, '2026-11-11T00:00:00Z')",
            (ciphertext_revision,),
        )
        db.commit()
        counts = export_metadata(source, target)

    assert counts == {"analytics_client_daily": 1, "submission_audit": 1}
    with sqlite3.connect(target) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "runtime_config" not in tables
        assert "runtime_config_revisions" not in tables
        assert tables == {"analytics_client_daily", "submission_audit"}
        assert db.execute("SELECT count FROM analytics_client_daily").fetchone() == (5,)
        assert db.execute(
            "SELECT channel_mode, model_name, fallback_used, client_device, client_os, client_browser "
            "FROM submission_audit"
        ).fetchone() == ("free", "gpt-4o-mini", 1, "desktop", "Windows", "Chrome")
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    raw_bytes = target.read_bytes()
    assert ciphertext_current.encode() not in raw_bytes
    assert ciphertext_revision.encode() not in raw_bytes
    assert b"v1:" not in raw_bytes
