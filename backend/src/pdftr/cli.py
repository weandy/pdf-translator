"""开发用命令行：在流水线（M4）完成前单独运行解析与融合。

uv run pdftr-dev parse paper.pdf --out work/        # 调用 MinerU 云端（需 PDFTR_MINERU_API_TOKEN）
uv run pdftr-dev fuse paper.pdf work/layout.json --out work/ir.json
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from pdftr.config import Settings
from pdftr.domain.ids import new_job_id
from pdftr.extraction.chars import extract
from pdftr.fusion.build import fuse
from pdftr.parsing.mineru_cloud import MinerUCloudParser
from pdftr.parsing.mineru_normalize import normalize


def _parse(args: argparse.Namespace) -> None:
    out = Path(args.out)
    parser = MinerUCloudParser(Settings().mineru_api_token)
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
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
