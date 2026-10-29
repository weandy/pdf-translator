"""版面解析端口。

解析服务（MinerU 云端、Doc2X、本地 MinerU）各自实现 LayoutParser，
输出统一的 domain.layout.LayoutResult；融合模块只认 LayoutResult，不关心来自哪个服务。
"""

from pathlib import Path
from typing import Protocol

from pdftr.domain.layout import LayoutResult


class ParseError(RuntimeError):
    """解析服务调用失败（网络、鉴权、额度、服务端错误等），消息面向用户。

    retryable：网络超时、连接失败、上传停滞、服务端 5xx 这类过一会儿可能就好的错误，流水线会自动重试；
    Token 无效、额度用完、文件被拒等重试也没用的为 False。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ParseProgress(Protocol):
    def __call__(self, message: str, fraction: float | None = None) -> None:
        """面向用户的状态说明，以及可选的完成比例（0–1）。"""
        ...


class LayoutParser(Protocol):
    name: str
    is_cloud: bool  # 为真时，前端必须取得用户的上传确认

    async def parse(
        self,
        pdf: Path,
        pages: list[int] | None = None,
        raw_dir: Path | None = None,
        on_progress: ParseProgress | None = None,
    ) -> LayoutResult:
        """解析 PDF。pages 为 0 起的页码（None 表示全部）；raw_dir 不为空时把服务原始结果存到这里，便于重放；
        on_progress 在服务端状态变化时被调用（排队、解析到第几页等）。"""
        ...
