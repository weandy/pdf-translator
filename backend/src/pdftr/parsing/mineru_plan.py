"""规划 MinerU 待解析页及本地连续切片。"""

import logging
import re
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Literal, cast

import pymupdf

from pdftr.parsing.port import ParseError

NATIVE_PAGE_RANGES_MAX_BYTES = 10 * 1024 * 1024
MIB = 1024 * 1024
log = logging.getLogger(__name__)
_SECTION = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+[A-Z]|[A-Z]\.\s+[A-Z]|第\s*\d+\s*章)"
    r"|^(?:Appendix|References|Bibliography|Acknowledg(?:e)?ments?)\b",
)
_ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|et al|Fig|Eq|Sec|Dr|vs|No|Vol|pp)\.$", re.IGNORECASE)


@dataclass(frozen=True)
class ShardSpec:
    index: int
    orig_pages: tuple[int, ...]
    use_source_pdf: bool
    page_ranges: str | None = None


@dataclass(frozen=True)
class ParsePlan:
    mode: Literal["full_single", "native_ranges", "local_shards"]
    total_pdf_pages: int | None
    requested_pages: tuple[int, ...] | None
    shards: tuple[ShardSpec, ...]

    @property
    def total_selected_pages(self) -> int | None:
        return len(self.requested_pages) if self.requested_pages is not None else self.total_pdf_pages


def _runs(pages: Sequence[int]) -> list[tuple[int, ...]]:
    return [tuple(page for _, page in run) for _, run in groupby(enumerate(pages), lambda pair: pair[1] - pair[0])]


def format_page_ranges(pages: Sequence[int]) -> str:
    return ",".join(str(run[0] + 1) if len(run) == 1 else f"{run[0] + 1}-{run[-1] + 1}" for run in _runs(pages))


def _text_blocks(page: pymupdf.Page) -> list[tuple[float, float, float, float, str, int, int]]:
    return cast(list[tuple[float, float, float, float, str, int, int]], page.get_text("blocks"))


def is_likely_cover_page(page: pymupdf.Page) -> bool:
    text = " ".join(block[4] for block in _text_blocks(page) if block[6] == 0)
    chars = sum(not c.isspace() for c in text)
    words = re.findall(r"[A-Za-z]+|[\u3400-\u9fff]", text)
    return 1 <= chars < 150 and len(words) < 30 and any(span["type"] != 3 for span in page.get_texttrace())


def compute_requested_pages(
    total_pages: int, selected_pages: list[int] | None, first_page_is_cover: bool
) -> tuple[int, ...] | None:
    if selected_pages is None:
        return None
    if not selected_pages or any(type(p) is not int or not 0 <= p < total_pages for p in selected_pages):
        raise ParseError("页码范围无效")
    selected = set(selected_pages)
    if 0 not in selected:
        selected.add(0)
        if first_page_is_cover and total_pages >= 2:
            selected.add(1)
    return None if total_pages <= 6 or len(selected) >= total_pages - 2 else tuple(sorted(selected))


@contextmanager
def _open_pdf(pdf: Path) -> Iterator[pymupdf.Document]:
    try:
        doc = pymupdf.open(pdf)
    except (pymupdf.FileDataError, RuntimeError):
        raise ParseError("无法打开 PDF 文件，请确认文件完整有效") from None
    with doc:
        if doc.needs_pass:
            raise ParseError("PDF 已加密，请先移除密码")
        if not doc.is_pdf or doc.page_count < 1:
            raise ParseError("无法打开 PDF 文件，请确认文件完整有效")
        yield doc


def _requested(doc: pymupdf.Document, pages: list[int] | None) -> tuple[int, ...] | None:
    cover = pages is not None and 0 not in pages and doc.page_count > 6 and is_likely_cover_page(doc[0])
    return compute_requested_pages(doc.page_count, pages, cover)


def preview_requested_pages(pdf: Path, pages: list[int] | None) -> tuple[int, ...] | None:
    if pages is None:
        return None
    with _open_pdf(pdf) as doc:
        return _requested(doc, pages)


def _source_plan(total: int | None, requested: tuple[int, ...] | None = None) -> ParsePlan:
    if requested is not None:
        return ParsePlan(
            "native_ranges", total, requested, (ShardSpec(0, requested, True, format_page_ranges(requested)),)
        )
    return ParsePlan("full_single", total, None, (ShardSpec(0, tuple(range(total or 0)), True),))


def build_parse_plan(
    pdf: Path,
    pages: list[int] | None,
    *,
    enable_partial_pages: bool = False,
    enable_sharding: bool = False,
    shard_threshold: int = 24,
    shard_target_pages: int = 14,
    shard_min_pages: int = 8,
    max_shards: int = 6,
) -> ParsePlan:
    if not enable_sharding and (not enable_partial_pages or pages is None):
        return _source_plan(None)
    with _open_pdf(pdf) as doc:
        total = doc.page_count
        requested = _requested(doc, pages) if enable_partial_pages else None
        runs = _runs(requested) if requested is not None else [tuple(range(total))]
        should_split = enable_sharding and (len(requested) if requested is not None else total) >= shard_threshold
        if requested is None and not should_split:
            return _source_plan(total)
        if len(runs) > max_shards or (
            requested is not None
            and not should_split
            and len(runs) == 1
            and pdf.stat().st_size <= NATIVE_PAGE_RANGES_MAX_BYTES
        ):
            return _source_plan(total, requested)
        if should_split:
            runs = _split_runs(doc, runs, shard_threshold, shard_target_pages, shard_min_pages, max_shards)
        if requested is None and len(runs) == 1:
            return _source_plan(total)
        return ParsePlan(
            "local_shards", total, requested, tuple(ShardSpec(i, run, False) for i, run in enumerate(runs))
        )


def _split_runs(
    doc: pymupdf.Document, runs: list[tuple[int, ...]], threshold: int, target: int, minimum: int, limit: int
) -> list[tuple[int, ...]]:
    desired = [
        max(1, min(len(run) // minimum, max(2, round(len(run) / target)))) if len(run) >= threshold else 1
        for run in runs
    ]
    quotas = [1] * len(runs)
    for _ in range(limit - len(runs)):
        candidates = (i for i, count in enumerate(quotas) if count < desired[i])
        index = max(candidates, key=lambda i: len(runs[i]) / quotas[i], default=None)
        if index is None:
            break
        quotas[index] += 1
    cache: dict[int, tuple[str, str] | None] = {}
    return [
        part for run, count in zip(runs, quotas, strict=True) for part in _split_run(doc, run, count, minimum, cache)
    ]


def _split_run(
    doc: pymupdf.Document, run: tuple[int, ...], count: int, minimum: int, cache: dict[int, tuple[str, str] | None]
) -> list[tuple[int, ...]]:
    result = []
    previous = 0
    for part in range(1, count):
        ideal = round(part * len(run) / count)
        lower = previous + minimum
        upper = len(run) - (count - part) * minimum
        candidates = range(max(lower, ideal - 2), min(upper, ideal + 2) + 1)
        cut = max(
            candidates,
            key=lambda c: (_boundary_score(doc, run[c - 1], run[c], cache), -abs(c - ideal)),
            default=min(upper, max(lower, ideal)),
        )
        result.append(run[previous:cut])
        previous = cut
    result.append(run[previous:])
    return result


def _boundary_lines(
    doc: pymupdf.Document, index: int, cache: dict[int, tuple[str, str] | None]
) -> tuple[str, str] | None:
    if index in cache:
        return cache[index]
    page = doc[index]
    height, width = page.rect.height, page.rect.width
    rotation = page.rotation_matrix
    blocks = []
    for x0, y0, x1, y1, text, _, kind in _text_blocks(page):
        if kind == 0 and text.strip():
            # PyMuPDF 的文本坐标未旋转，变换后再使用可见页面的边界与栏位置。
            rect = pymupdf.Rect(x0, y0, x1, y1) * rotation
            if rect.y0 >= 0.08 * height and rect.y1 <= 0.92 * height:
                blocks.append((rect, text))
    cache[index] = None
    if sum(len(text.strip()) for _, text in blocks) >= 40 and any(span["type"] != 3 for span in page.get_texttrace()):
        two_columns = any(rect.x1 <= 0.53 * width for rect, _ in blocks) and any(
            rect.x0 >= 0.47 * width for rect, _ in blocks
        )
        blocks.sort(key=lambda b: (two_columns and b[0].x1 > 0.53 * width, b[0].y0, b[0].x0))
        head = blocks[0][1].strip().splitlines()[0].strip()[:200]
        tail = blocks[-1][1].strip().splitlines()[-1].strip()[-200:]
        cache[index] = (head, tail)
    return cache[index]


def _boundary_score(
    doc: pymupdf.Document, previous: int, following: int, cache: dict[int, tuple[str, str] | None]
) -> int:
    left, right = _boundary_lines(doc, previous, cache), _boundary_lines(doc, following, cache)
    if left is None or right is None:
        return 0
    tail, head = left[1], right[0]
    score = 3 if _SECTION.match(head) else 0
    if tail.endswith((".", "?", "!", "。", "？", "！")) and not _ABBREVIATION.search(tail):
        score += 2
    lower = head[:1].islower()
    if not lower and not head.startswith((",", ";", ":", ")", "]")):
        score += 1
    if lower or tail.endswith(("-", "\u00ad", ",")):
        score -= 2
    return score


def materialize_shards(pdf: Path, plan: ParsePlan, attempt_dir: Path) -> tuple[ParsePlan, tuple[Path, ...]]:
    if plan.mode != "local_shards":
        return plan, (pdf,)
    orig_size = pdf.stat().st_size
    if shutil.disk_usage(attempt_dir).free < max(128 * MIB, orig_size * 2):
        log.warning("MinerU 临时空间不足，回退原文件上传")
        return _source_plan(plan.total_pdf_pages, plan.requested_pages), (pdf,)
    paths = []
    total = 0
    with _open_pdf(pdf) as src:
        for shard in plan.shards:
            path = attempt_dir / f"doc_{shard.index}.pdf"
            paths.append(path)
            with pymupdf.open() as dst:
                dst.insert_pdf(src, from_page=shard.orig_pages[0], to_page=shard.orig_pages[-1])
                dst.save(path, garbage=4, deflate=True)
            size = path.stat().st_size
            total += size
            if size > 200 * MIB or (total > max(64 * MIB, orig_size * 3) and total > 256 * MIB):
                for path in paths:
                    path.unlink()
                log.warning("MinerU 临时切片超过大小预算，回退原文件上传")
                return _source_plan(plan.total_pdf_pages, plan.requested_pages), (pdf,)
    return plan, tuple(paths)
