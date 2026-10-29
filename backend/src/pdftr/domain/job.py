"""任务记录与选项。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from pdftr.domain.ir import Quality


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"  # 用户取消：保留在“我的任务”里，可以从中断处重新开始或删除


# 流水线阶段，按执行顺序排列（docs/03-mvp-design.md §3.4）
STAGES = ("parse", "fuse", "segment", "glossary", "translate", "render")
Stage = Literal["parse", "fuse", "segment", "glossary", "translate", "render", "done"]


class JobOptions(BaseModel):
    # 版面解析服务：MinerU（有行级与行内公式坐标）。PaddleOCR-VL 只保留后端适配器、界面不开放（解析慢）
    parser: Literal["mineru-cloud", "paddleocr-vl"] = "mineru-cloud"
    quality: Quality = "default"
    include_references: bool = False
    # 翻译图里的矢量文字（坐标轴、图例、框图标签）。默认关闭：图表里的文字保留原文，只翻图表标题（界面不开放）
    translate_figures: bool = False
    pages: list[int] | None = None  # 0 起的页码；None 表示全部
    cloud_consent: bool = False  # 用户已确认文件会上传到云端解析服务
    # 用户自己的术语库（保存在浏览器里，随任务发来）：优先于自动抽取的术语与公共词库
    user_terms: dict[str, str] = Field(default_factory=dict, max_length=5000)


class Job(BaseModel):
    id: str
    filename: str
    status: JobStatus = JobStatus.QUEUED
    stage: Stage = "parse"
    progress: float = 0.0  # 当前阶段内 0–1
    message: str = ""
    error: str = ""
    page_count: int = 0
    options: JobOptions = Field(default_factory=JobOptions)
    model: str = ""
    cost_cny: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stage_started_at: datetime | None = None  # 当前阶段开始时间，前端据此显示“已等待多久”
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
