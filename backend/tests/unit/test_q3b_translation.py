import argparse
import asyncio
import json
from pathlib import Path

from pdftr import cli
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, Page, Segment
from pdftr.domain.job import Job
from pdftr.domain.llm import ChatOptions, ChatResult
from pdftr.pipeline.secrets import Secrets
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore
from pdftr.translation.batching import placeholder_problems
from pdftr.translation.prompt import PROMPT_VERSION, RULES
from pdftr.translation.service import TranslateReport


def test_v5_preserves_formula_binding_when_order_changes():
    assert PROMPT_VERSION == "v6"
    assert "跟随原本对应的概念或实体一起移动" in RULES
    assert "编号不必递增" in RULES
    assert "Self-attention requires <m1>O(1)</m1> sequential operations" in RULES
    assert "自注意力只需<m1/>次顺序操作，而循环层需要<m2/>次，因此自注意力可以大幅提高并行度。" in RULES
    assert "The weight <m1>w</m1> of the sample <m2>s</m2>" in RULES
    assert "样本<m2/>的权重<m1/>固定" in RULES
    assert "跨学科多义词按本文领域的通行学术译法统一" in RULES
    assert "禁止半中半英夹译" in RULES
    assert placeholder_problems("The weight <m1/> of the sample <m2/> is fixed.", "样本<m2/>的权重<m1/>固定。") == []
    assert placeholder_problems("The weight <m1/> of the sample <m2/> is fixed.", "样本<m2/>的权重固定。")


class _Chat:
    model = "synthetic"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        self.prompts.append(user)
        return ChatResult(
            text="领域：测量\nAI公共词库：停用\nsilver chamber=银室\nk-means=均值聚类\njournal=期刊\ncold fusion=冷聚变"
        )


def test_glossary_candidates_reach_model_through_runner_and_cli(tmp_path: Path, monkeypatch):
    job = Job(id="0123456789abcdef", filename="synthetic.pdf")
    segs = [
        Segment(id=f"s{i}", job_id=job.id, block_ids=[f"p0-b{i}"], source=text)
        for i, text in enumerate(
            ["K-means", "Cold", "fusion"]
            + ["Silver chambers. Silver chamber."] * 5
            + ["Journal proceedings volume issue."] * 6
        )
    ]
    box = Rect.of([70, 100, 220, 113])
    blocks = [
        Block(
            id=s.block_ids[0],
            page=0,
            type=BlockType.TITLE if i == 0 else BlockType.REFERENCE if i >= 8 else BlockType.TEXT,
            bbox=box,
            order=i,
            style=BlockStyle(font_size=10),
            translatable=True,
        )
        for i, s in enumerate(segs)
    ]
    doc = Document(job_id=job.id, pages=[Page(index=0, width=600, height=800, blocks=blocks)])
    runner_chat = _Chat()
    pipeline = build_container(
        Settings(data_dir=tmp_path, glossary_sources=""), deps_override={"chat_factory": lambda _: runner_chat}
    ).pipeline
    deps = pipeline.deps
    out = deps.artifacts.path(job.id, "glossary.json")
    try:
        deps.artifacts.path(job.id, "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
        deps.store.save_job(job)
        deps.store.replace_segments(job.id, segs)
        asyncio.run(pipeline._glossary(job, Secrets("", "", "", "")))
    finally:
        assert isinstance(deps.store, SqliteStore)
        assert isinstance(deps.cache, SqliteTranslationCache)
        deps.store.close()
        deps.cache.close()
    assert json.loads(out.read_text(encoding="utf-8"))["terms"] == {"Silver chambers": "银室"}

    layout_path = tmp_path / "layout.json"
    layout_path.write_text("{}", encoding="utf-8")
    cli_chat = _Chat()
    monkeypatch.setattr(cli, "Settings", lambda: Settings(data_dir=tmp_path, llm_protocol="openai"))
    monkeypatch.setattr(cli, "normalize", lambda _: None)
    monkeypatch.setattr(cli, "extract", lambda _: [])
    monkeypatch.setattr(cli, "fuse", lambda *args, **kwargs: (doc, None))
    monkeypatch.setattr(cli, "segment", lambda *args, **kwargs: segs)
    monkeypatch.setattr(cli, "OpenAICompatChat", lambda **kwargs: cli_chat)

    async def translate(*args, **kwargs):
        return segs, TranslateReport()

    monkeypatch.setattr(cli, "translate", translate)
    asyncio.run(
        cli._translate_async(
            argparse.Namespace(layout=layout_path, pdf=job.filename, refs=False, quality="default", out=None)
        )
    )
    assert runner_chat.prompts == cli_chat.prompts
    assert len(cli_chat.prompts) == 1
    assert "章节标题（仅供判断领域）：K-means" in cli_chat.prompts[0]
    assert cli_chat.prompts[0].endswith("：Silver、Silver chambers、chambers")
