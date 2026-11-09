"""失败段的重试上限与任务状态、费用、原有译文保持一致。"""

import asyncio
import re
from pathlib import Path

import pytest

from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.ir import Segment, SegmentStatus
from pdftr.domain.job import Job, JobStatus
from pdftr.domain.llm import ChatOptions, ChatResult, ChatUsage
from pdftr.pipeline import runner, usecases
from pdftr.pipeline.secrets import Secrets
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore
from pdftr.translation.prompt import DocContext

SECRETS = Secrets("https://example.invalid", "test-key", "deepseek-flash", "test-token")


class RepairChat:
    model = "deepseek-flash"

    def __init__(self) -> None:
        self.broken = True
        self.calls: list[str] = []

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        self.calls.append(user)
        lines = []
        for line in user.splitlines():
            if match := re.match(r"^(\d+)\|(.*)$", line):
                text = re.sub(r"<(m\d+)>.*?</\1>", r"<\1/>", match.group(2))
                if self.broken:
                    text = text.replace("<m1/>", "")
                lines.append(f"{match.group(1)}|合成译文：{text}")
        return ChatResult(text="\n".join(lines), usage=ChatUsage(prompt_tokens=100, completion_tokens=50))

    async def list_models(self) -> list[str]:
        return [self.model]


def setup_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat: RepairChat):
    container = build_container(Settings(data_dir=tmp_path, glossary_sources=""), {"chat_factory": lambda _: chat})
    pipeline = container.pipeline
    store = pipeline.deps.store
    monkeypatch.setattr(runner, "load_context", lambda *args: DocContext())
    monkeypatch.setattr(usecases, "load_context", lambda *args: DocContext())

    async def render(job, secrets):
        pass

    monkeypatch.setattr(pipeline, "_render", render)
    job = Job(id="synthetic", filename="synthetic.pdf", stage="translate")
    store.save_job(job)
    store.replace_segments(
        job.id,
        [
            Segment(id="hard", job_id=job.id, block_ids=["b1"], source="Hard <m1/> paragraph.", formulas={"m1": "x"}),
            Segment(id="easy", job_id=job.id, block_ids=["b2"], source="Easy paragraph."),
        ],
    )
    return pipeline, job


def close_pipeline(pipeline):
    assert isinstance(pipeline.deps.store, SqliteStore)
    assert isinstance(pipeline.deps.cache, SqliteTranslationCache)
    pipeline.deps.store.close()
    pipeline.deps.cache.close()


def test_incomplete_translation_is_not_done_and_resume_only_translates_failed(tmp_path, monkeypatch):
    chat = RepairChat()
    pipeline, job = setup_pipeline(tmp_path, monkeypatch, chat)

    async def scenario():
        pipeline.submit(job, SECRETS)
        assert await pipeline.wait_all(3)
        await asyncio.sleep(0)
        failed = pipeline.deps.store.get_job(job.id)
        assert failed is not None and failed.status is JobStatus.FAILED and failed.stage == "translate"
        assert "未完成翻译" in failed.error and failed.cost_cny is not None and failed.cost_cny > 0
        hard, easy = pipeline.deps.store.segments(job.id)
        assert hard.status is SegmentStatus.FAILED and hard.translation_attempts == 10 and hard.failure_reason
        assert easy.status is SegmentStatus.DONE
        cost = failed.cost_cny
        before_calls = len(chat.calls)
        chat.broken = False
        usecases.retry_job(pipeline, job.id, SECRETS)
        assert await pipeline.wait_all(3)
        recovered = pipeline.deps.store.get_job(job.id)
        assert recovered is not None and recovered.status is JobStatus.DONE
        assert recovered.cost_cny is not None and recovered.cost_cny > cost
        assert all(s.status is SegmentStatus.DONE for s in pipeline.deps.store.segments(job.id))
        assert len(chat.calls) == before_calls + 1 and "Easy paragraph" not in chat.calls[-1]

    try:
        asyncio.run(scenario())
    finally:
        close_pipeline(pipeline)


def test_manual_retry_has_whole_request_budget_and_retains_previous_valid_translation(tmp_path, monkeypatch):
    chat = RepairChat()
    pipeline, job = setup_pipeline(tmp_path, monkeypatch, chat)
    original = pipeline.deps.store.segment("hard")
    assert original is not None
    original.status, original.target = SegmentStatus.EDITED, "原有可用译文 <m1/>"
    pipeline.deps.store.save_segments([original])
    real_translate = usecases.translate
    observed = []

    async def translate(*args, **kwargs):
        observed.append(kwargs["opts"].total_timeout_s)
        return await real_translate(*args, **kwargs)

    monkeypatch.setattr(usecases, "translate", translate)
    try:
        result = asyncio.run(usecases.retranslate_segment(pipeline, "hard", "default", SECRETS))
        assert observed == [45]
        assert result.status is SegmentStatus.EDITED and result.target == original.target
        assert result.failure_reason and result.translation_attempts == 10
        current = pipeline.deps.store.get_job(job.id)
        assert current is not None and current.cost_cny is not None and current.cost_cny > 0
    finally:
        close_pipeline(pipeline)
