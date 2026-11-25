"""任务工件的版本路径与延迟回收。"""

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from pdftr.domain.job import Job
from pdftr.storage.port import ArtifactStore

log = logging.getLogger(__name__)
VERSION_GRACE_S = 900


def version_artifact(artifacts: ArtifactStore, job_id: str, version: int, name: str) -> Path:
    if version <= 1:
        return artifacts.path(job_id, name)
    # 读历史版本不会重建已回收的目录。
    return artifacts.job_dir(job_id) / "versions" / f"v{version}" / name


def candidate_dir(artifacts: ArtifactStore, job_id: str, op_id: str) -> Path:
    return artifacts.job_dir(job_id) / "expansions" / op_id


def discard_candidate(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    except OSError:
        log.warning("候选工件清理失败，将在下轮重试")


def purge_versions(artifacts: ArtifactStore, job: Job, now: datetime) -> None:
    root = artifacts.root / job.id
    if not root.is_dir() or root.is_symlink():
        return
    cutoff = now.timestamp() - VERSION_GRACE_S
    versions = root / "versions"
    candidates = root / "expansions"
    # 以替代版本发布时的新目录为时间基准，不能在首次发布后立刻删除很早之前生成的 v1。
    for parent in (versions, candidates):
        if not parent.is_dir() or parent.is_symlink():
            continue
        for path in parent.iterdir():
            if parent == versions:
                match = re.fullmatch(r"v([2-9][0-9]*|1[0-9]+)", path.name)
                if match is None or int(match[1]) == job.content_version:
                    continue
                if int(match[1]) < job.content_version:
                    successor = versions / f"v{int(match[1]) + 1}"
                    if successor.is_dir() and successor.stat().st_mtime >= cutoff:
                        continue
            elif not re.fullmatch(r"[0-9a-f]{32}", path.name):
                continue
            if not path.is_symlink() and path.stat().st_mtime < cutoff:
                discard_candidate(path)
    if job.content_version < 2:
        return
    first_replacement = versions / "v2"
    if first_replacement.is_dir() and first_replacement.stat().st_mtime >= cutoff:
        return
    for path in [*(root / name for name in ("layout.json", "ir.json", "background.pdf")), *root.glob("links.v*.json")]:
        if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
