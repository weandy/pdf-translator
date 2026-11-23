"""等待后台工件工作完成后，再将调用方的取消向上传递。"""

import asyncio
from collections.abc import Callable


async def finish_on_cancel[T](task: asyncio.Task[T], on_cancel: Callable[[], None] | None = None) -> T:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        # 重复取消也不能让调用方在工作线程退出前释放槽位或关闭文件。
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def run_thread[**P, T](work: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    return await finish_on_cancel(asyncio.create_task(asyncio.to_thread(work, *args, **kwargs)))
