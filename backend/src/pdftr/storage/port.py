"""存储端口：任务工件文件、任务、段落与翻译缓存。"""

from pathlib import Path
from typing import Protocol

from pdftr.domain.ir import Segment, SegmentStatus


class ArtifactStore(Protocol):
    def job_dir(self, job_id: str) -> Path: ...

    def path(self, job_id: str, name: str) -> Path: ...


class SegmentRepo(Protocol):
    def save_many(self, segments: list[Segment]) -> None: ...

    def by_job(self, job_id: str) -> list[Segment]: ...

    def pending(self, job_id: str) -> list[Segment]: ...

    def update_target(self, segment_id: str, target: str, status: SegmentStatus) -> None: ...


class CacheRepo(Protocol):
    def get(self, key: str) -> str | None: ...

    def put(self, key: str, target: str) -> None: ...
