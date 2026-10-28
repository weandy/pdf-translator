"""任务记录与选项。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from pdftr.domain.ir import Quality


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# 流水线阶段，按执行顺序排列（docs/03-mvp-design.md §3.4）
STAGES = ("parse", "fuse", "segment", "glossary", "translate", "render")
Stage = Literal["parse", "fuse", "segment", "glossary", "translate", "render", "done"]


class JobOptions(BaseModel):
    parser: Literal["mineru-cloud"] = "mineru-cloud"
    quality: Quality = "default"
    include_references: bool = False
    pages: list[int] | None = None  # 0 起的页码；None 表示全部
    cloud_consent: bool = False  # 用户已确认文件会上传到云端解析服务


class Job(BaseModel):
    id: str
    filename: str
    status: JobStatus = JobStatus.QUEUED
    stage: Stage = "parse"
    progress: float = 0.0  # 当前阶段内 0–1
    message: str = ""
    error: str = ""
    page_count: int = 0
    options: JobOptions = Field(default_factory=JobOptions)
    model: str = ""
    cost_cny: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
