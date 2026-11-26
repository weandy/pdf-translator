"""批次 D 生产入口守护测试：确认未达标前生产仍严守 v7 默认行为、缓存语义与双入口一致。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from pdftr import cli
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, Page, Segment, SegmentStatus
from pdftr.domain.job import Job, JobOptions, JobStatus
from pdftr.domain.llm import ChatOptions, ChatResult
from pdftr.glossary.extract import PROMPT as EXTRACT_PROMPT
from pdftr.pipeline.secrets import Secrets
from pdftr.pipeline.usecases import retranslate_segments
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore
from pdftr.translation.batching import VALIDATION_VERSION
from pdftr.translation.prompt import PROMPT_VERSION, RULES
from pdftr.translation.service import TranslateReport, cache_key

_BASELINE_EXTRACT_SHA = "ee4f67a94676b2eab3fc34e9bfdea5df34c7142ece9e9d9289c6bed1f3821550"
_BASELINE_RULES_SHA = "eb9a1afc2aa1f9c4c2011b9be366270538b656d7e14e22a3a30e28d5c9609556"
_BASELINE_CACHE_GOLD = "14c5408c8783c2b4e7d0910dc43749963374a40cedb5e155981e9f496cda6f3d"


def test_production_keeps_v7_prompts_and_invalidates_the_baseline_cache() -> None:
    assert PROMPT_VERSION == "v7"
    assert VALIDATION_VERSION == 8
    assert hashlib.sha256(EXTRACT_PROMPT.encode("utf-8")).hexdigest() == _BASELINE_EXTRACT_SHA
    assert hashlib.sha256(RULES.encode("utf-8")).hexdigest() == _BASELINE_RULES_SHA
    gold_seg = Segment(id="s0", job_id="j0", block_ids=["p0-b0"], source="Inspect the sample.")
    assert cache_key(gold_seg, "synthetic-model", {}, "default", validation_version=7) == _BASELINE_CACHE_GOLD
    current_key = cache_key(gold_seg, "synthetic-model", {}, "default")
    assert current_key == cache_key(gold_seg, "synthetic-model", {}, "default", validation_version=8)
    assert current_key != _BASELINE_CACHE_GOLD


class _RecordingChat:
    model = "synthetic-v7"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        self.calls.append((system, user))
        return ChatResult(text=self.reply)


def _synthetic_doc(job_id: str, segs: list[Segment]) -> Document:
    box = Rect.of([70, 100, 260, 120])
    blocks = [
        Block(
            id=s.block_ids[0],
            page=0,
            type=BlockType.TEXT,
            bbox=box,
            order=i,
            style=BlockStyle(font_size=10),
            translatable=True,
        )
        for i, s in enumerate(segs)
    ]
    return Document(job_id=job_id, pages=[Page(index=0, width=600, height=800, blocks=blocks)])


def test_runner_reuses_existing_glossary_skips_done_edited_and_retranslate_bypasses_cache(tmp_path: Path) -> None:
    job = Job(
        id="b47cd00000000001",
        filename="synthetic.pdf",
        status=JobStatus.DONE,
        options=JobOptions(user_terms={"priority": "调度优先级", "queues": "任务队列"}),
    )
    segs = [
        Segment(
            id="s0",
            job_id=job.id,
            block_ids=["p0-b0"],
            source="Priority rules govern task dispatch.",
            target="已有手工编辑译文。",
            status=SegmentStatus.EDITED,
            role=BlockType.TEXT,
            section="Method",
        ),
        Segment(
            id="s1",
            job_id=job.id,
            block_ids=["p0-b1"],
            source="Priority rules apply to queues.",
            target="",
            status=SegmentStatus.PENDING,
            role=BlockType.TEXT,
            section="Method",
        ),
    ]
    doc = _synthetic_doc(job.id, segs)
    chat = _RecordingChat("1|调度优先级规则适用于队列。\n</translations>")
    pipeline = build_container(
        Settings(data_dir=tmp_path, glossary_sources=""),
        deps_override={"chat_factory": lambda _: chat},
    ).pipeline
    deps = pipeline.deps
    try:
        deps.artifacts.path(job.id, "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
        # 预置历史 glossary.json：验证 _glossary 直接复用且不发起模型抽取
        glossary_path = deps.artifacts.path(job.id, "glossary.json")
        glossary_path.write_text(
            json.dumps({"domain": "调度", "terms": {"priority": "旧词"}, "use_library": False}, ensure_ascii=False),
            encoding="utf-8",
        )
        deps.store.save_job(job)
        deps.store.replace_segments(job.id, segs)

        asyncio.run(pipeline._glossary(job, Secrets("", "", "", "")))
        assert chat.calls == []

        # _translate 跳过 EDITED，仅翻译 PENDING；同键用户词覆盖 system_prompt，额外用户词按段注入 user_prompt
        asyncio.run(pipeline._translate(job, Secrets("", "", "", "")))
        assert len(chat.calls) == 1
        assert "priority=调度优先级" in chat.calls[0][0]
        assert "旧词" not in chat.calls[0][0]
        assert "queues=任务队列" in chat.calls[0][1]
        saved = {s.id: s for s in deps.store.segments(job.id)}
        assert saved["s0"].target == "已有手工编辑译文。"
        assert saved["s1"].target == "调度优先级规则适用于队列。"

        # 第二次对重置为 PENDING 的 s1 调用 _translate 命中缓存（0 新增请求）
        deps.store.replace_segments(
            job.id,
            [saved["s0"], saved["s1"].model_copy(update={"status": SegmentStatus.PENDING, "target": ""})],
        )
        asyncio.run(pipeline._translate(job, Secrets("", "", "", "")))
        assert len(chat.calls) == 1

        # 显式重翻走 use_cache=False，必须再次调用模型且不破坏原缓存内容
        chat.reply = "1|队列均遵循调度优先级规则。\n</translations>"
        retranslated = asyncio.run(
            retranslate_segments(
                pipeline,
                job.id,
                ["s1"],
                "default",
                Secrets(
                    llm_api_key="dummy-key",
                    llm_base_url="https://api.example.invalid/v1",
                    llm_model="synthetic-v7",
                    parser_token="",
                ),
            )
        )
        assert len(chat.calls) == 2
        assert retranslated[0].target == "队列均遵循调度优先级规则。"
    finally:
        assert isinstance(deps.store, SqliteStore)
        assert isinstance(deps.cache, SqliteTranslationCache)
        deps.store.close()
        deps.cache.close()


def test_cli_translate_runs_in_isolated_data_dir_with_v7_baseline(tmp_path: Path, monkeypatch) -> None:
    job_id = "b47cd00000000002"
    segs = [
        Segment(
            id="s0",
            job_id=job_id,
            block_ids=["p0-b0"],
            source="We study cycle latency and queueing delay in synthetic clusters across multiple runs.",
            role=BlockType.TEXT,
            section="Intro",
        )
    ]
    doc = _synthetic_doc(job_id, segs)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text("{}", encoding="utf-8")
    out_path = tmp_path / "translated.json"
    cli_chat = _RecordingChat("领域：计算机系统\nAI公共词库：停用\ncycle latency=周期延迟")
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "Settings", lambda: Settings(data_dir=tmp_path, llm_protocol="openai"))
    monkeypatch.setattr(cli, "normalize", lambda _: None)
    monkeypatch.setattr(cli, "extract", lambda _: [])
    monkeypatch.setattr(cli, "fuse", lambda *args, **kwargs: (doc, None))
    monkeypatch.setattr(cli, "segment", lambda *args, **kwargs: segs)
    monkeypatch.setattr(cli, "OpenAICompatChat", lambda **kwargs: cli_chat)

    async def _fake_translate(items, **kwargs):
        captured["ctx"] = kwargs["ctx"]
        captured["count"] = len(items)
        return [items[0].model_copy(update={"target": "译文。", "status": SegmentStatus.DONE})], TranslateReport(
            total=1, translated=1
        )

    monkeypatch.setattr(cli, "translate", _fake_translate)
    cli._translate(
        argparse.Namespace(
            pdf="synthetic.pdf", layout=str(layout_path), refs=False, quality="default", out=str(out_path)
        )
    )

    assert len(cli_chat.calls) == 1
    assert captured["count"] == 1
    assert out_path.exists()
    assert (tmp_path / "app.db").exists()
