"""检查受控合成数据的主库清理效果；不将此验证解释为磁盘或 WAL 安全擦除。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from pdftr.domain.ir import Segment
from pdftr.domain.job import Job
from pdftr.domain.submissions import SubmissionSearch
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore


def test_purge_overwrites_freed_main_database_cells_in_jobs_segments_and_cache(tmp_path: Path):
    path = tmp_path / "app.db"
    store = SqliteStore(path)
    cache = SqliteTranslationCache(path)
    sentinel = "synthetic-private-cell-marker-" * 400
    try:
        current = Job(id="0000000000000001", filename=sentinel)
        store.create_job(current)
        store.replace_segments(current.id, [Segment(id="s1", job_id=current.id, block_ids=[], source=sentinel)])
        cache.put("synthetic-key", sentinel)
        with cache._db:
            cache._db.execute("UPDATE translation_cache SET created_at = '2000-01-01 00:00:00'")
        store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert sentinel.encode()[:500] in path.read_bytes()
        store.delete_job(current.id)
        assert cache.purge_older_than(24) == 1
        store.checkpoint_after_purge()
        assert sentinel.encode()[:500] not in path.read_bytes()
        assert store.get_job(current.id) is None and not store.segments(current.id)
        assert cache.get("synthetic-key") is None
    finally:
        cache.close()
        store.close()


def test_invalid_ip_error_message_does_not_echo_search_value():
    sensitive = "synthetic-sensitive-search"
    with pytest.raises(ValidationError) as failure:
        SubmissionSearch(ip=sensitive)
    # HTTP 错误只允许 type/loc/msg；字段自身的 msg 同样不得夹带原始值。
    assert all(sensitive not in error["msg"] for error in failure.value.errors())


def test_parser_tmp_purge_is_independent_of_retention(tmp_path):
    import os

    from pdftr.config import Settings
    from pdftr.container import build_container
    from pdftr.pipeline.usecases import purge_expired
    from pdftr.storage.files import FileArtifactStore

    pipeline = build_container(Settings(data_dir=tmp_path, retention_hours=0)).pipeline
    artifacts = FileArtifactStore(tmp_path)
    job = "0123456789abcdef"
    root = artifacts.job_dir(job) / "parser-tmp"
    old, fresh = root / "attempt-old", root / "attempt-fresh"
    old.mkdir(parents=True)
    fresh.mkdir()
    os.utime(old, (1, 1))
    assert artifacts.purge_parser_tmp({job}) == 0
    purge_expired(pipeline, 0)
    assert not old.exists() and fresh.exists()
    pipeline.recover_interrupted()
    assert not root.exists()


def test_parser_tmp_cleanup_respects_age_of_empty_parent(tmp_path):
    import os

    from pdftr.storage.files import FileArtifactStore

    artifacts = FileArtifactStore(tmp_path)
    path = artifacts.job_dir("0123456789abcdef") / "parser-tmp"
    path.mkdir()
    assert artifacts.purge_parser_tmp(set(), max_age_s=600) == 0
    assert path.is_dir()
    os.utime(path, (1, 1))
    assert artifacts.purge_parser_tmp(set(), max_age_s=600) == 0
    assert not path.exists()
