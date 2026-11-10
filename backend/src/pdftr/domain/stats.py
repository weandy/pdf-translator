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
