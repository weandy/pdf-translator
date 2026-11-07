"""站点使用统计：仅提供匿名聚合计数，不包含任务内容或访客标识。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class StatsCounts(BaseModel):
    page_views: int = 0
    visitors: int = 0
    jobs_submitted: int = 0
    jobs_completed: int = 0
    pages_submitted: int = 0


class StatsDay(StatsCounts):
    date: str


class StatsOut(BaseModel):
    since: datetime
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    total: StatsCounts
    today: StatsCounts
    daily: list[StatsDay]
