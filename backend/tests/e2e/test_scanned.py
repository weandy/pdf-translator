"""扫描件：带隐形 OCR 层的扫描页用解析服务的识别结果翻译，背景页用纸色盖住原文。"""

import json
import re
import time
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType
from pdftr.domain.layout import LayoutBlock, LayoutLine, LayoutPage, LayoutResult, LayoutSpan
from pdftr.domain.llm import ChatOptions, ChatResult

HEADERS = {"X-LLM-Key": "k", "X-Parser-Key": "t"}


class FakeChat:
    """把每行原文变成“译:原文”。"""

    model = "deepseek-flash"

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        if "抽取需要全文统一译法的专业术语" in user:
            return ChatResult(text="领域：测试")
        lines = [ln.split("|", 1) for ln in user.splitlines() if "|" in ln and ln.split("|", 1)[0].isdigit()]
        return ChatResult(text="\n".join(f"{n}|译:{re.sub(r'<(m\d+)>.*?</\1>', r'<\1/>', t)}" for n, t in lines))

    async def list_models(self) -> list[str]:
        return [self.model]


TEXT = "Scanned paragraphs keep their meaning when the parser reads the page image."
BLOCK = Rect(x0=60, y0=100, x1=420, y1=140)
PAPER = (0.96, 0.93, 0.85)  # 发黄的纸


def make_scan() -> bytes:
    """一页图片（发黄的纸上印着字）+ 一层质量很差的隐形 OCR 文字，与网上常见的扫描版论文一样。"""
    printed = pymupdf.open()
    page = printed.new_page(width=500, height=300)
    page.draw_rect(page.rect, color=None, fill=PAPER)
    page.insert_textbox(pymupdf.Rect(60, 100, 420, 140), TEXT, fontsize=11, fontname="tiro")
    image = page.get_pixmap(dpi=150).tobytes("png")
    scan = pymupdf.open()
    out = scan.new_page(width=500, height=300)
    out.insert_image(out.rect, stream=image)
    out.insert_text((60, 112), "Scamied paragrapbs kecp tlieir rneaning wlien tbe parscr", fontsize=11, render_mode=3)
    return scan.tobytes()


class ScanParser:
    name = "fake"
    is_cloud = True

    async def parse(self, pdf: Path, pages=None, raw_dir=None, on_progress=None) -> LayoutResult:
        # 识别结果只有块级坐标；行内公式也没有自己的位置
        spans = [LayoutSpan(kind="text", content=TEXT + " With", bbox=BLOCK),
                 LayoutSpan(kind="inline_formula", content="x^2", bbox=BLOCK)]  # fmt: skip
        block = LayoutBlock(type=BlockType.TEXT, bbox=BLOCK, order=0, lines=[LayoutLine(bbox=BLOCK, spans=spans)])
        return LayoutResult(parser="fake", pages=[LayoutPage(index=0, width=500, height=300, blocks=[block])])


def test_scanned_page_is_translated_over_a_paper_coloured_cover(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, glossary_sources="", font_dir=tmp_path / "fonts")
    container = build_container(
        settings, {"parser_factory": lambda s, n: ScanParser(), "chat_factory": lambda s: FakeChat()}
    )
    with TestClient(create_app(container)) as client:
        files = {"file": ("scan.pdf", make_scan(), "application/pdf")}
        job = client.post("/api/jobs", files=files, data={"options": json.dumps({"cloud_consent": True})},
                          headers=HEADERS).json()  # fmt: skip
        for _ in range(100):
            job = client.get(f"/api/jobs/{job['id']}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job

        doc = client.get(f"/api/jobs/{job['id']}/document").json()
        (seg,) = doc["segments"]
        assert doc["pages"][0]["kind"] == "scanned"
        # 原文用解析服务的识别结果，不用隐形 OCR 层的乱码；公式没有原图位置
        assert seg["source"].startswith("Scanned paragraphs") and "Scamied" not in seg["source"]
        assert seg["formulas"]["m1"] == {"latex": "x^2", "page": None, "bbox": None}

        bg = client.get(f"/api/jobs/{job['id']}/files/background.pdf")
        with pymupdf.open(stream=bg.content, filetype="pdf") as pdf:
            page = pdf[0]
            assert "Scamied" not in page.get_text()  # 隐形 OCR 文字删掉了
            pix = page.get_pixmap(dpi=72, clip=pymupdf.Rect(BLOCK.x0, BLOCK.y0, BLOCK.x1, BLOCK.y1))
            dark = sum(1 for i in range(0, len(pix.samples), pix.n) if pix.samples[i] < 128)
            assert dark == 0  # 原文被盖住，没有深色笔画
            r, _, b = pix.pixel(pix.width // 2, pix.height // 2)[:3]
            assert abs(r / 255 - PAPER[0]) < 0.05 and abs(b / 255 - PAPER[2]) < 0.05  # 盖的是纸色，不是白色

        mono = client.get(f"/api/jobs/{job['id']}/export", params={"mode": "mono"})
        with pymupdf.open(stream=mono.content, filetype="pdf") as pdf:
            assert "译:Scanned" in str(pdf[0].get_text()).replace("\n", "")
