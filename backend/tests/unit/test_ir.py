from pdftr.domain.geometry import Rect
from pdftr.domain.ids import block_id, new_job_id
from pdftr.domain.ir import Block, BlockStyle, BlockType, Document, Page, Segment, SegmentStatus


def _doc() -> Document:
    style = BlockStyle(font_size=10)
    blocks = [
        Block(
            id=block_id(0, 0),
            page=0,
            type=BlockType.TITLE,
            bbox=Rect.of([50, 50, 500, 70]),
            order=0,
            style=style,
            translatable=True,
        ),
        Block(
            id=block_id(0, 1),
            page=0,
            type=BlockType.FORMULA,
            bbox=Rect.of([100, 100, 400, 130]),
            order=1,
            latex=r"E = mc^2",
            style=style,
            translatable=False,
        ),
    ]
    return Document(job_id=new_job_id(), pages=[Page(index=0, width=595, height=842, blocks=blocks)])


def test_document_lookup_and_roundtrip():
    doc = _doc()
    assert doc.block("p0-b1") is not None
    assert doc.block("p0-b1").latex == r"E = mc^2"  # type: ignore[union-attr]
    assert doc.block("p9-b9") is None
    # IR 落盘为 JSON 后能原样读回
    assert Document.model_validate_json(doc.model_dump_json()) == doc


def test_segment_defaults():
    seg = Segment(id="j-s0", job_id="j", block_ids=["p0-b0"], source="Let <m1/> be")
    assert seg.status is SegmentStatus.PENDING
    assert seg.quality == "default"
    assert seg.target is None


def test_job_id_format():
    jid = new_job_id()
    assert len(jid) == 16
    assert int(jid, 16) >= 0
