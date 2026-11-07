"""进度推送：订阅窗口和漏消息后仍能收敛到数据库中的最终状态。"""

import asyncio
import json
from collections.abc import AsyncGenerator, Coroutine
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request

from pdftr.api import routes_jobs
from pdftr.domain.job import Job, JobStatus
from pdftr.pipeline.events import EventBus
from pdftr.pipeline.runner import Pipeline
from pdftr.pipeline.usecases import NotFound


class MemoryStore:
    def __init__(self) -> None:
        self.job = Job(id="0000000000000001", filename="synthetic.pdf", status=JobStatus.RUNNING)

    def get_job(self, job_id: str) -> Job | None:
        return self.job.model_copy(deep=True) if job_id == self.job.id else None

    def finish(self) -> Job:
        self.job = self.job.model_copy(update={"status": JobStatus.DONE, "stage": "done", "progress": 1.0})
        return self.job


def setup_stream():
    store = MemoryStore()
    events = EventBus()
    pipeline = cast(Pipeline, SimpleNamespace(deps=SimpleNamespace(store=store, events=events)))

    async def connected() -> bool:
        return False

    request = cast(Request, SimpleNamespace(is_disconnected=connected))
    return store, events, pipeline, request


def status(chunk: str) -> str:
    assert chunk.startswith("data: "), chunk
    return json.loads(chunk.removeprefix("data: "))["status"]


def test_finish_between_response_creation_and_subscription_is_not_lost():
    async def scenario():
        store, events, pipeline, request = setup_stream()
        response = await routes_jobs.job_events(store.job.id, request, pipeline)
        # 响应对象建立后，ASGI 尚未开始迭代响应体：此时完成事件没有订阅者。
        events.publish(store.finish())
        stream = cast(AsyncGenerator[str], response.body_iterator)
        try:
            assert status(await anext(stream)) == "done"
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        finally:
            await stream.aclose()
        assert not events._subs

    asyncio.run(scenario())


def test_heartbeat_recovers_done_without_published_event(monkeypatch: pytest.MonkeyPatch):
    async def timeout(awaitable: Coroutine[object, object, Job], timeout: float) -> Job:
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(routes_jobs, "asyncio", SimpleNamespace(wait_for=timeout))

    async def scenario():
        store, events, pipeline, request = setup_stream()
        response = await routes_jobs.job_events(store.job.id, request, pipeline)
        stream = cast(AsyncGenerator[str], response.body_iterator)
        try:
            assert status(await anext(stream)) == "running"
            # 模拟事件漏发或其他进程仅更新了数据库，订阅队列没有收到 done。
            store.finish()
            assert status(await anext(stream)) == "done"
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        finally:
            await stream.aclose()
        assert not events._subs

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", [JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED])
def test_published_terminal_event_closes_and_unsubscribes(terminal: JobStatus):
    async def scenario():
        store, events, pipeline, request = setup_stream()
        response = await routes_jobs.job_events(store.job.id, request, pipeline)
        stream = cast(AsyncGenerator[str], response.body_iterator)
        assert status(await anext(stream)) == "running"
        events.publish(store.job.model_copy(update={"status": terminal}))
        assert status(await anext(stream)) == terminal
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not events._subs

    asyncio.run(scenario())


def test_disconnected_client_unsubscribes():
    async def disconnected() -> bool:
        return True

    async def scenario():
        store, events, pipeline, _ = setup_stream()
        request = cast(Request, SimpleNamespace(is_disconnected=disconnected))
        response = await routes_jobs.job_events(store.job.id, request, pipeline)
        stream = cast(AsyncGenerator[str], response.body_iterator)
        assert status(await anext(stream)) == "running"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not events._subs

    asyncio.run(scenario())


def test_unknown_job_fails_before_response_starts():
    async def scenario():
        _, _, pipeline, request = setup_stream()
        with pytest.raises(NotFound):
            await routes_jobs.job_events("missing", request, pipeline)

    asyncio.run(scenario())
