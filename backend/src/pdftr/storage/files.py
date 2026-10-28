"""任务工件文件：data/jobs/<job_id>/<name>。"""

import re
import shutil
from pathlib import Path

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-/]+$")


class FileArtifactStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "jobs"

    def job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", job_id):
            raise ValueError(f"非法的任务 ID：{job_id!r}")
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def remove(self, job_id: str) -> None:
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    def path(self, job_id: str, name: str) -> Path:
        # 工件名由程序生成，这里仍做校验，防止路径穿越
        if not _SAFE_NAME.fullmatch(name) or ".." in Path(name).parts:
            raise ValueError(f"非法的工件名：{name!r}")
        p = self.job_dir(job_id) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
