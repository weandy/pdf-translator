"""坐标与矩形。

全项目统一使用 PDF point、左上角原点（与 PyMuPDF 一致）。
MinerU 的 content_list 使用 0–1000 归一化坐标，layout.json 使用 point（见 docs/02-spikes.md S2）。
"""

from pydantic import BaseModel, ConfigDict


class Rect(BaseModel):
    model_config = ConfigDict(frozen=True)

    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def of(cls, box: list[float] | tuple[float, ...]) -> "Rect":
        x0, y0, x1, y1 = box
        return cls(x0=x0, y0=y0, x1=x1, y1=y1)

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2

    def contains_point(self, x: float, y: float, tol: float = 0.0) -> bool:
        return self.x0 - tol <= x <= self.x1 + tol and self.y0 - tol <= y <= self.y1 + tol

    def intersection(self, other: "Rect") -> "Rect | None":
        x0, y0 = max(self.x0, other.x0), max(self.y0, other.y0)
        x1, y1 = min(self.x1, other.x1), min(self.y1, other.y1)
        if x0 >= x1 or y0 >= y1:
            return None
        return Rect(x0=x0, y0=y0, x1=x1, y1=y1)

    def shrink(self, d: float) -> "Rect":
        """向内收缩 d（去字时避免误删相邻字符，见 S5）。"""
        return Rect(x0=self.x0 + d, y0=self.y0 + d, x1=self.x1 - d, y1=self.y1 - d)


def from_normalized(box: list[float], page_width: float, page_height: float) -> Rect:
    """0–1000 归一化坐标 → PDF point。"""
    x0, y0, x1, y1 = box
    return Rect(
        x0=x0 / 1000 * page_width,
        y0=y0 / 1000 * page_height,
        x1=x1 / 1000 * page_width,
        y1=y1 / 1000 * page_height,
    )
