"""生成回归测试样本：下载 5 篇 arXiv 论文，并用 MinerU 云端解析。

  cd backend
  PDFTR_MINERU_API_TOKEN=... uv run python scripts/fetch_samples.py

结果写到 backend/.samples/（已在 .gitignore 中）。论文原文与解析结果不入库。
"""

import asyncio
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

from pdftr.config import Settings
from pdftr.parsing.mineru_cloud import MinerUCloudParser

PAPERS = ["1412.6980", "1512.03385", "1706.03762", "1810.04805", "2605.10845"]
OUT = Path(__file__).resolve().parents[1] / ".samples"


async def main() -> None:
    token = Settings().mineru_api_token
    if not token:
        sys.exit("请设置环境变量 PDFTR_MINERU_API_TOKEN")
    OUT.mkdir(exist_ok=True)
    parser = MinerUCloudParser(token)
    for paper in PAPERS:
        pdf = OUT / f"{paper}.pdf"
        if not pdf.exists():
            print(f"下载 {paper}")
            urllib.request.urlretrieve(f"https://arxiv.org/pdf/{paper}", pdf)
        layout = OUT / f"{paper}.layout.json"
        if layout.exists():
            continue
        print(f"解析 {paper}")
        raw = OUT / f"{paper}.raw"
        await parser.parse(pdf, raw_dir=raw)
        with zipfile.ZipFile(io.BytesIO((raw / "mineru.zip").read_bytes())) as z:
            name = next(n for n in z.namelist() if n.endswith("layout.json"))
            layout.write_text(json.dumps(json.loads(z.read(name)), ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
