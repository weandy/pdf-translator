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
