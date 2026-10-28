"""版面解析端口。

解析服务（MinerU 云端、Doc2X、本地 MinerU）各自实现 LayoutParser，
输出统一的 domain.layout.LayoutResult；融合模块只认 LayoutResult，不关心来自哪个服务。
"""

from pathlib import Path
from typing import Protocol

from pdftr.domain.layout import LayoutResult


class ParseError(RuntimeError):
    """解析服务调用失败（网络、鉴权、额度、服务端错误等），消息面向用户。"""


class LayoutParser(Protocol):
    name: str
    is_cloud: bool  # 为真时，前端必须取得用户的上传确认

    async def parse(self, pdf: Path, pages: list[int] | None = None, raw_dir: Path | None = None) -> LayoutResult:
        """解析 PDF。pages 为 0 起的页码（None 表示全部）；raw_dir 不为空时把服务原始结果存到这里，便于重放。"""
        ...
