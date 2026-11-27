"""测试 MinerU 图表/表格坐标高清 PNG 裁剪接口。"""

from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, Page, PageKind
from pdftr.domain.job import Job, JobStatus


def _seed_job_with_figure(tmp_path: Path) -> tuple[TestClient, str]:
    container = build_container(Settings(data_dir=tmp_path))
    deps = container.pipeline.deps
    job_id = "ab1234567890cdef"
    pdf_path = deps.artifacts.path(job_id, "original.pdf")
    pdf = pymupdf.open()
    page = pdf.new_page(width=400, height=500)
    page.draw_rect(pymupdf.Rect(50, 60, 250, 220), color=(0.1, 0.3, 0.5), fill=(0.85, 0.92, 0.96), width=2)
    page.insert_text((80, 140), "Synthetic Figure", fontsize=14, color=(0.1, 0.1, 0.1))
    pdf.save(pdf_path)
    pdf.close()

    doc = Document(
        job_id=job_id,
        pages=[
            Page(
                index=0,
                width=400,
                height=500,
                kind=PageKind.NATIVE,
                blocks=[
                    Block(
                        id="p0-fig1",
                        page=0,
                        type=BlockType.FIGURE,
                        bbox=Rect(x0=50, y0=60, x1=250, y1=220),
                        order=0,
                        style=BlockStyle(font_size=10),
                        translatable=False,
                    ),
                    Block(
                        id="p0-text1",
                        page=0,
                        type=BlockType.TEXT,
                        bbox=Rect(x0=50, y0=250, x1=350, y1=300),
                        order=1,
                        style=BlockStyle(font_size=10),
                        translatable=True,
                    ),
                ],
            )
        ],
    )
    deps.artifacts.path(job_id, "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
    deps.store.create_job(Job(id=job_id, filename="paper.pdf", status=JobStatus.DONE, stage="done", page_count=1))
    return TestClient(create_app(container)), job_id


def test_block_png_crops_figure_and_rejects_text(tmp_path: Path) -> None:
    client, job_id = _seed_job_with_figure(tmp_path)

    res = client.get(f"/api/jobs/{job_id}/blocks/p0-fig1.png")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(res.content) > 500
    assert "content-disposition" not in res.headers

    dl = client.get(f"/api/jobs/{job_id}/blocks/p0-fig1.png?download=true")
    assert dl.status_code == 200
    assert 'attachment; filename="p0-fig1.png"' in dl.headers.get("content-disposition", "")

    # 非图表块（如普通文本块）拒绝裁剪，防止被任意扫描截取正文
    denied = client.get(f"/api/jobs/{job_id}/blocks/p0-text1.png")
    assert denied.status_code == 404

    missing = client.get(f"/api/jobs/{job_id}/blocks/p0-nope.png")
    assert missing.status_code == 404
