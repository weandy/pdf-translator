"""验证编辑段落时支持同步修改与校验行内公式 LaTeX（策略 1：更新网页与复制用 LaTeX，导出保持原版公式裁剪图）。"""

from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import Container, build_container
from pdftr.domain.geometry import Rect
from pdftr.domain.ir import (
    Block,
    BlockStyle,
    BlockType,
    DocMeta,
    Document,
    FormulaRef,
    InlineFormula,
    Page,
    PageKind,
    Segment,
    SegmentPart,
    SegmentStatus,
)
from pdftr.domain.job import Job, JobStatus


def _seed_formula_job(container: Container) -> str:
    store = container.pipeline.deps.store
    artifacts = container.pipeline.deps.artifacts
    job_id = "f000000000000001"
    pdf = pymupdf.open()
    pdf.new_page(width=595, height=842)
    pdf.save(artifacts.path(job_id, "original.pdf"))
    pdf.close()
    rect = Rect(x0=72, y0=100, x1=420, y1=140)
    fbox = Rect(x0=150, y0=108, x1=170, y1=120)
    block = Block(
        id="p0-b0",
        page=0,
        type=BlockType.TEXT,
        bbox=rect,
        order=0,
        formulas=[InlineFormula(key="m1", latex="d_1", bbox=fbox)],
        style=BlockStyle(font_size=10),
        translatable=True,
    )
    doc = Document(
        job_id=job_id,
        pages=[Page(index=0, width=595, height=842, kind=PageKind.NATIVE, blocks=[block])],
        meta=DocMeta(title="Formula Edit Test"),
    )
    artifacts.path(job_id, "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
    store.create_job(Job(id=job_id, filename="test.pdf", status=JobStatus.DONE, stage="done", page_count=1))
    store.replace_segments(
        job_id,
        [
            Segment(
                id="seg-f1",
                job_id=job_id,
                block_ids=["p0-b0"],
                source="Dimension <m1/> vector.",
                target="维度为 <m1/> 的向量。",
                formulas={"m1": "d_1"},
                formula_refs={"m1": FormulaRef(page=0, bbox=fbox)},
                parts=[SegmentPart(block_id="p0-b0", chars=23)],
                status=SegmentStatus.DONE,
            )
        ],
    )
    return "seg-f1"


def test_patch_segment_updates_formula_latex_and_rejects_unknown_key(tmp_path: Path) -> None:
    container = build_container(Settings(data_dir=tmp_path))
    app = create_app(container)
    with TestClient(app) as client:
        seg_id = _seed_formula_job(container)

        # 1. 仅修改公式 LaTeX（文字不变），保存成功并持久化
        resp = client.patch(
            f"/api/segments/{seg_id}",
            json={"target": "维度为 <m1/> 的向量。", "formulas": {"m1": "d_k"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "edited"
        assert body["formulas"]["m1"]["latex"] == "d_k"

        # 2. 再次通过 document 接口读取，确认持久化生效且原图坐标未丢
        doc_resp = client.get("/api/jobs/f000000000000001/document")
        assert doc_resp.status_code == 200
        seg_out = doc_resp.json()["segments"][0]
        assert seg_out["formulas"]["m1"]["latex"] == "d_k"
        assert seg_out["formulas"]["m1"]["bbox"] is not None

        # 3. 传入不存在的公式 key 返回 400
        bad_key = client.patch(
            f"/api/segments/{seg_id}",
            json={"target": "维度为 <m1/> 的向量。", "formulas": {"m9": "x^2"}},
        )
        assert bad_key.status_code == 400

        # 4. 传入空白 LaTeX 返回 422
        empty_latex = client.patch(
            f"/api/segments/{seg_id}",
            json={"target": "维度为 <m1/> 的向量。", "formulas": {"m1": "   "}},
        )
        assert empty_latex.status_code == 422
