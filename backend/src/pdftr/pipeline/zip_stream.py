"""已知大小与 CRC 的 PDF 工件可直接组成 ZIP32，无需压缩或生成临时 ZIP。"""

import asyncio
import struct
from collections.abc import AsyncGenerator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import BinaryIO

from pdftr.pipeline.workers import finish_on_cancel

CHUNK_BYTES = 64 * 1024
ZIP32_LIMIT = 0xFFFFFFFF
_UTF8 = 0x0800
_DOS_DATE = 33  # 1980-01-01


@dataclass(frozen=True)
class ZipEntry:
    name: bytes
    size_bytes: int
    crc32: int
    file: BinaryIO


def zip_size(entries: Sequence[tuple[bytes, int]]) -> int:
    return sum(30 + len(name) + size + 46 + len(name) for name, size in entries) + 22


async def pool_call[**P, T](pool: ThreadPoolExecutor, work: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    def run() -> T:
        return work(*args, **kwargs)

    async def wait() -> T:
        return await asyncio.get_running_loop().run_in_executor(pool, run)

    return await finish_on_cancel(asyncio.create_task(wait()))


async def stream_zip(entries: Sequence[ZipEntry], pool: ThreadPoolExecutor) -> AsyncGenerator[bytes]:
    center = []
    offset = 0
    for entry in entries:
        name, size, crc = entry.name, entry.size_bytes, entry.crc32
        yield struct.pack("<I5H3I2H", 0x04034B50, 20, _UTF8, 0, 0, _DOS_DATE, crc, size, size, len(name), 0) + name
        left = size
        while left:
            chunk = await pool_call(pool, entry.file.read, min(left, CHUNK_BYTES))
            if not chunk:
                raise OSError("读取导出快照时文件提前结束")
            left -= len(chunk)
            yield chunk
        entry.file.close()
        center.append(
            struct.pack(
                "<I6H3I5H2I",
                0x02014B50,
                20,
                20,
                _UTF8,
                0,
                0,
                _DOS_DATE,
                crc,
                size,
                size,
                len(name),
                0,
                0,
                0,
                0,
                0,
                offset,
            )
            + name
        )
        offset += 30 + len(name) + size
    yield from_center(center, offset)


def from_center(center: list[bytes], offset: int) -> bytes:
    directory = b"".join(center)
    return directory + struct.pack("<I4H2IH", 0x06054B50, 0, 0, len(center), len(center), len(directory), offset, 0)
