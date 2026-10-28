"""把处理 PDF 后空出来的内存还给系统。

Python 与 MuPDF 释放的内存多半留在进程里等下次复用，服务常驻时看起来一直占着。实测处理 6 篇论文后
进程 242 MB，调用 malloc_trim 后回到 158 MB（PyMuPDF 自身缓存清不清影响不大）。只在 glibc 上有效，其他平台什么也不做。
"""

import ctypes
import ctypes.util
import gc

_libc = None
try:
    if name := ctypes.util.find_library("c"):
        _libc = ctypes.CDLL(name)
        _libc.malloc_trim  # noqa: B018  musl 等没有这个函数，访问时抛 AttributeError
except (OSError, AttributeError):
    _libc = None


def release_memory() -> None:
    gc.collect()
    if _libc is not None:
        _libc.malloc_trim(0)
