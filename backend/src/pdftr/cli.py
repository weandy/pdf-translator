"""开发用命令行：在流水线（M4）完成前单独运行解析与融合。

uv run pdftr-dev parse paper.pdf --out work/        # 调用 MinerU 云端（需 PDFTR_MINERU_API_TOKEN）
uv run pdftr-dev fuse paper.pdf work/layout.json --out work/ir.json
uv run pdftr-dev translate paper.pdf work/layout.json --out work/segments.json   # 需 PDFTR_LLM_API_KEY
"""

import argparse
import asyncio
import json
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from pdftr.config import Settings
from pdftr.domain.ids import new_job_id
from pdftr.extraction.chars import extract
from pdftr.fusion.build import fuse
from pdftr.glossary import sources as glossary_sources
from pdftr.glossary.extract import extract_acronym_definitions, extract_terms, frequent_candidates
from pdftr.glossary.matcher import TermMatcher, TermSelection
from pdftr.glossary.normalize import normalize_terms
from pdftr.parsing.mineru_cloud import MinerUCloudParser
from pdftr.parsing.mineru_normalize import normalize
from pdftr.pipeline.runner import chat_session, glossary_texts
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.translation.anthropic_compat import AnthropicChat
from pdftr.translation.openai_compat import OpenAICompatChat
from pdftr.translation.prompt import DocContext
from pdftr.translation.segmenter import segment
from pdftr.translation.service import TranslateOptions, default_concurrency, translate


def _parse(args: argparse.Namespace) -> None:
    out = Path(args.out)
    settings = Settings()
    parser = MinerUCloudParser(settings.mineru_api_token, proxy=settings.mineru_proxy)
    asyncio.run(parser.parse(Path(args.pdf), raw_dir=out))
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO((out / "mineru.zip").read_bytes())) as z:
        name = next(n for n in z.namelist() if n.endswith("layout.json"))
        (out / "layout.json").write_bytes(z.read(name))
    print(f"已保存 {out / 'layout.json'}")


def _fuse(args: argparse.Namespace) -> None:
    layout = normalize(json.loads(Path(args.layout).read_text(encoding="utf-8")))
    doc, report = fuse(new_job_id(), layout, extract(Path(args.pdf)), used_cloud=True)
    if args.out:
        Path(args.out).write_text(doc.model_dump_json(), encoding="utf-8")
    stats = asdict(report) | {"coverage": round(report.coverage, 4), "title": doc.meta.title[:80]}
    print(json.dumps(stats, ensure_ascii=False))


def _translate(args: argparse.Namespace) -> None:
    # 整个流程放在同一个事件循环里：异步 HTTP 客户端的连接池绑定在创建它的事件循环上
    asyncio.run(_translate_async(args))


async def _translate_async(args: argparse.Namespace) -> None:
    settings = Settings()
    layout = normalize(json.loads(Path(args.layout).read_text(encoding="utf-8")))
    doc, _ = fuse(new_job_id(), layout, extract(Path(args.pdf)), used_cloud=True)
    segments = segment(doc, include_references=args.refs)
    body_sources, headings = glossary_texts(doc, segments)
    intro = next((s.source for s in segments if len(s.source.split()) >= 30 and s.source != doc.meta.abstract), "")

    def load_library() -> TermMatcher:
        glossary, _ = glossary_sources.load_all(
            glossary_sources.parse_ids(settings.glossary_sources), settings.data_dir / "glossaries" / "vendor"
        )
        return TermMatcher(glossary)

    chat_class = AnthropicChat if settings.llm_protocol == "anthropic" else OpenAICompatChat
    model = chat_class(base_url=settings.llm_base_url, api_key=settings.llm_api_key, model=settings.llm_model)
    candidates = frequent_candidates(body_sources)
    definitions = extract_acronym_definitions(body_sources, candidates)
    async with chat_session(model) as chat:
        found = await extract_terms(
            chat, title=doc.meta.title, abstract=doc.meta.abstract, intro=intro, headings=headings,
            candidates=candidates, definitions=definitions,
        )  # fmt: skip
        found.terms = normalize_terms(found.terms, body_sources, headings)
        selection = TermSelection(found, {}, load_library)
        ctx = DocContext(title=doc.meta.title, abstract=doc.meta.abstract, domain=found.domain, terms=selection.paper)
        concurrency = settings.llm_concurrency or default_concurrency(settings.llm_base_url)
        progress = lambda done, total, note="": print(f"\r翻译 {done}/{total} {note}", end="", flush=True)  # noqa: E731
        with closing(SqliteTranslationCache(settings.data_dir / "app.db")) as cache:
            out, report = await translate(
                segments, chat=chat, cache=cache, ctx=ctx,
                lookup_term_hits=selection.match_segment, context_segments=segments,
                opts=TranslateOptions(quality=args.quality, concurrency=concurrency), on_progress=progress,
            )  # fmt: skip
    print()
    if args.out:
        Path(args.out).write_text(json.dumps([s.model_dump() for s in out], ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps({
        "segments": len(out), "domain": found.domain, "terms": len(selection.paper),
        "library_entries": selection.library_size,
        "report": report.model_dump(), "term_extraction_usage": found.usage.model_dump(),
    }, ensure_ascii=False, indent=1))  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="pdftr-dev")
    sub = ap.add_subparsers(required=True)
    p = sub.add_parser("parse", help="调用 MinerU 云端解析")
    p.add_argument("pdf")
    p.add_argument("--out", required=True)
    p.set_defaults(func=_parse)
    f = sub.add_parser("fuse", help="融合版面结果与 PDF 字符，生成 IR")
    f.add_argument("pdf")
    f.add_argument("layout")
    f.add_argument("--out")
    f.set_defaults(func=_fuse)
    t = sub.add_parser("translate", help="融合并翻译（需 PDFTR_LLM_API_KEY）")
    t.add_argument("pdf")
    t.add_argument("layout")
    t.add_argument("--quality", choices=["default", "careful"], default="default")
    t.add_argument("--refs", action="store_true", help="同时翻译参考文献")
    t.add_argument("--out")
    t.set_defaults(func=_translate)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
