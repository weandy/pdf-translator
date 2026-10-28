"""按 token 计价（S7：DeepSeek 按 token 计费，单价与思考强度无关，只与时段有关）。"""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from pdftr.domain.llm import ChatUsage

_BEIJING = timezone(timedelta(hours=8))


class Price(BaseModel):
    """元 / 百万 token（空闲时段）；高峰时段乘以 peak_factor。"""

    cache_hit: float
    cache_miss: float
    output: float
    peak_factor: float = 1.0


# 2026-10-28 官网价格（docs/00-research.md、02-spikes.md S7）
PRICES: dict[str, Price] = {
    "deepseek-flash": Price(cache_hit=0.02, cache_miss=1.0, output=4.0, peak_factor=2.0),
    "deepseek-v4-pro": Price(cache_hit=0.15, cache_miss=4.5, output=13.5, peak_factor=2.0),
}


def is_deepseek_peak(now: datetime | None = None) -> bool:
    """DeepSeek 高峰：北京时间工作日 9–12 点、14–18 点（法定节假日未计入）。"""
    t = (now or datetime.now(_BEIJING)).astimezone(_BEIJING)
    if t.weekday() >= 5:
        return False
    return 9 <= t.hour < 12 or 14 <= t.hour < 18


def add_cost(total: float | None, model: str, usage: ChatUsage) -> float | None:
    """累加一次请求的费用：按请求发出时是否高峰计价（跨越高峰时段边界的任务也不会算错）。未知模型保持 None。"""
    cost = cost_cny(model, usage, peak=is_deepseek_peak())
    if cost is None:
        return total
    return (total or 0.0) + cost


def cost_cny(model: str, usage: ChatUsage, *, peak: bool) -> float | None:
    """未知模型返回 None（界面显示“按服务商计费”）。"""
    price = PRICES.get(model)
    if price is None:
        return None
    total = (
        usage.cache_hit_tokens * price.cache_hit
        + usage.cache_miss_tokens * price.cache_miss
        + usage.completion_tokens * price.output
    ) / 1e6
    return total * (price.peak_factor if peak else 1.0)
