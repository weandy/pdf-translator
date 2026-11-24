"""站点使用统计：仅提供匿名聚合计数，不包含任务内容或访客标识。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

PUBLIC_STATS_DAYS = 7


class StatsCounts(BaseModel):
    page_views: int = Field(default=0, ge=0)
    visitors: int = Field(default=0, ge=0)
    jobs_submitted: int = Field(default=0, ge=0)
    jobs_completed: int = Field(default=0, ge=0)
    pages_submitted: int = Field(default=0, ge=0)


class StatsDay(StatsCounts):
    date: str
    available: bool


class StatsPeriod(BaseModel):
    start_date: str
    end_date: str
    counts: StatsCounts


class StatsOut(BaseModel):
    since: datetime
    generated_at: datetime
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    period: StatsPeriod
    today: StatsCounts
    daily: list[StatsDay] = Field(min_length=PUBLIC_STATS_DAYS, max_length=PUBLIC_STATS_DAYS)


class ClientBucket(BaseModel):
    label: str
    count: int = Field(ge=0)
    share: float = Field(ge=0.0, le=1.0)


class ClientBreakdown(BaseModel):
    total: int = Field(default=0, ge=0)
    devices: list[ClientBucket] = Field(default_factory=list)
    platforms: list[ClientBucket] = Field(default_factory=list)


class OverviewFailureSummary(BaseModel):
    """总览页近 24 小时失败任务脱敏摘要（不含完整 IP 与原始 User-Agent）。"""

    job_id: str
    title: str
    submitted_at: datetime
    channel_mode: Literal["byok", "free"] | None = None
    failure_reason: str
    retryable: bool | None = None


class AdminStatsSnapshot(BaseModel):
    today_submitted: int = Field(default=0, ge=0)
    today_completed: int = Field(default=0, ge=0)
    period_submitted: int = Field(default=0, ge=0)
    period_completed: int = Field(default=0, ge=0)
    failed_24h: int = Field(default=0, ge=0)
    recent_failures: list[OverviewFailureSummary] = Field(default_factory=list)
    client_breakdown: ClientBreakdown = Field(default_factory=ClientBreakdown)
    client_breakdown_source: Literal["job", "visit"] = "job"
