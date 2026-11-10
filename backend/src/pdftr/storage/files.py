"""任务工件文件：data/jobs/<job_id>/<name>。"""

import re
import shutil
from contextlib import suppress
from datetime import datetime
from pathlib import Path

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-/]+$")


class FileArtifactStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "jobs"

    def job_dir(self, job_id: str) -> Path:
        d = self._checked_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _checked_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", job_id):
            raise ValueError(f"非法的任务 ID：{job_id!r}")
        d = self.root / job_id
        if d.is_symlink() or d.resolve().parent != self.root.resolve():
            raise ValueError("任务目录超出工件根目录")
        return d

    def remove(self, job_id: str) -> None:
        # 删除不创建目录，失败必须上抛，让持久化清理队列保留该任务。
        d = self._checked_dir(job_id)
        with suppress(FileNotFoundError):
            shutil.rmtree(d)
        if d.exists():
            raise OSError("任务目录尚未清理完成")

    def orphaned_jobs(self, known_job_ids: set[str], before: datetime) -> list[str]:
        if not self.root.exists():
            return []
        result = []
        for path in self.root.iterdir():
            if path.name in known_job_ids or not re.fullmatch(r"[0-9a-f]{16}", path.name):
                continue
            try:
                self._checked_dir(path.name)
                if path.is_dir() and path.stat().st_mtime < before.timestamp():
                    result.append(path.name)
            except (OSError, ValueError):
                continue
        return sorted(result)

    def path(self, job_id: str, name: str) -> Path:
        # 工件名由程序生成，这里仍做校验，防止路径穿越
        if not _SAFE_NAME.fullmatch(name) or ".." in Path(name).parts:
            raise ValueError(f"非法的工件名：{name!r}")
        p = self.job_dir(job_id) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
