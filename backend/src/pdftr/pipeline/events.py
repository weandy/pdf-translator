"""进程内的任务进度事件总线：流水线发布，SSE 接口订阅。"""

import asyncio
from collections import defaultdict

from pdftr.domain.job import Job


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Job]]] = defaultdict(set)

    def subscribe(self, job_id: str) -> asyncio.Queue[Job]:
        q: asyncio.Queue[Job] = asyncio.Queue(maxsize=100)
        self._subs[job_id].add(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue[Job]) -> None:
        self._subs[job_id].discard(q)
        if not self._subs[job_id]:
            del self._subs[job_id]

    def publish(self, job: Job) -> None:
        for q in list(self._subs.get(job.id, ())):
            if q.full():  # 订阅者太慢时丢掉最旧的事件，只需要最新状态
                q.get_nowait()
            q.put_nowait(job.model_copy())
