"""补翻操作的独立生命周期。"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ExpansionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_EXPANSION_STATUSES = (ExpansionStatus.QUEUED, ExpansionStatus.RUNNING)


class ExpansionStage(StrEnum):
    PARSE = "parse"
    FUSE = "fuse"
    SEGMENT = "segment"
    TRANSLATE = "translate"
    RENDER = "render"
    PUBLISH = "publish"
    DONE = "done"


class ExpansionOp(BaseModel):
    id: str
    job_id: str
    base_version: int
    target_version: int
    requested_pages: list[int]
    merged_pages: list[int]
    status: ExpansionStatus = ExpansionStatus.QUEUED
    stage: ExpansionStage = ExpansionStage.PARSE
    progress: float = 0.0
    message: str = "等待补翻"
    error: str = ""
    cost_cny: float | None = None
    cost_applied: bool = False
    fallback_used: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
