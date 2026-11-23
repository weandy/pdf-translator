"""PDF 写入同目录的独立临时文件，完成后原子发布。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile


@contextmanager
def pdf_destination(out: Path) -> Iterator[Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=out.parent, prefix=f".{out.stem}-", suffix=".part", delete=False) as file:
        tmp = Path(file.name)
    try:
        yield tmp
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
