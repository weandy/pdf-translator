"""提交元数据、可信请求上下文与受限查询，不包含论文正文或 HTTP 对象。"""

import ipaddress
import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from pdftr.domain.job import Job, JobStatus

NOTICE_VERSION = "submission-audit-v1"
IPSource = Literal["trusted_proxy", "direct", "unknown"]
TitleSource = Literal["parsed", "filename", "unknown"]
BEIJING = timezone(timedelta(hours=8))


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def normalize_ip(value: str) -> str:
    """统一 IPv4 映射地址，拒绝作用域、端口和不能作为完整地址的输入。"""
    if "%" in value:
        raise ValueError("IP 地址不能包含作用域")
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        # 校验错误可能直接变成 HTTP 字段提示，不能让 ipaddress 回显敏感筛选词。
        raise ValueError("IP 地址格式无效") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def clean_title(value: str, *, filename: bool = False) -> str:
    # 文件名可能来自另一种操作系统，不能只依赖本机 Path.name 去掉路径。
    if filename:
        value = re.split(r"[/\\]", value)[-1]
        value = re.sub(r"(?i)\.pdf$", "", value)
    value = "".join(c for c in value if unicodedata.category(c) not in {"Cc", "Cf"} or c.isspace())
    return " ".join(unicodedata.normalize("NFC", value).split())[:500]


class SubmissionContext(BaseModel):
    submitter_ip: str | None = None
    ip_source: IPSource = "unknown"
    notice_version: str | None = Field(default=None, max_length=100)

    @field_validator("submitter_ip")
    @classmethod
    def valid_ip(cls, value: str | None) -> str | None:
        return normalize_ip(value) if value is not None else None

    @model_validator(mode="after")
    def consistent_source(self) -> Self:
        if self.submitter_ip is None or self.ip_source == "unknown":
            self.submitter_ip, self.ip_source = None, "unknown"
        return self


class SubmissionRecord(BaseModel):
    job_id: str
    title: str
    title_source: TitleSource
    submitted_at: datetime
    submitter_ip: str | None
    ip_source: IPSource
    page_count: int = Field(ge=0)
    status: JobStatus
    first_completed_at: datetime | None
    updated_at: datetime
    record_source: Literal["live", "backfill"]
    notice_version: str | None
    current_stage: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    retryable: bool | None = None
    failure_updated_at: datetime | None = None


def failure_details(job: Job, now: datetime) -> tuple[str | None, str | None, bool | None, str | None]:
    """将任务错误映射为不含密钥、正文或模型原始响应的管理摘要。"""
    if job.status is not JobStatus.FAILED:
        return None, None, None, None
    raw = (job.error or "").lower()
    stage = job.stage
    if "服务重启" in raw:
        return "service_restart", "服务重启导致任务中断，可从断点重试", True, timestamp(now)
    if any(word in raw for word in ("api key", "鉴权", "认证", "401", "403", "密钥")):
        return (
            "llm_auth" if stage == "translate" else "parser_config",
            "服务凭据无效或未配置，请检查设置",
            False,
            timestamp(now),
        )
    if any(word in raw for word in ("余额", "额度", "quota", "限流", "rate limit", "429")):
        return (
            "llm_quota" if stage == "translate" else "parser_response",
            "服务额度不足或触发限流，请稍后重试",
            True,
            timestamp(now),
        )
    if any(word in raw for word in ("超时", "timeout", "网络", "连接", "network", "temporarily")):
        return (
            ("llm_network" if stage == "translate" else "parser_network"),
            "外部服务连接或响应超时，请稍后重试",
            True,
            timestamp(now),
        )
    if stage == "translate" and any(word in raw for word in ("校验", "尝试", "公式", "译文", "翻译")):
        return "translation_validation", "译文校验未通过，已达到尝试次数或时间上限", True, timestamp(now)
    if stage == "render":
        return "render_failed", "文档生成失败，可重试", True, timestamp(now)
    if stage in {"parse", "fuse", "segment", "glossary"}:
        return "parser_response", "版面解析结果无效或未找到可翻译文本", True, timestamp(now)
    return "system_error", "任务处理失败，可重试；如持续失败请检查服务日志", True, timestamp(now)


class SubmissionSearch(BaseModel):
    title_query: str = Field(default="", max_length=100)
    ip: str | None = Field(default=None, max_length=64)
    date_from: date | None = None
    date_to: date | None = None
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: Literal[25, 50, 100] = 25

    @field_validator("title_query")
    @classmethod
    def stripped_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("ip")
    @classmethod
    def valid_ip(cls, value: str | None) -> str | None:
        return normalize_ip(value) if value else None

    @model_validator(mode="after")
    def valid_dates(self) -> Self:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("起始日期不能晚于结束日期")
        if self.date_to == date.max:
            raise ValueError("结束日期超出支持范围")
        if self.date_from == date.min:
            raise ValueError("起始日期超出支持范围")
        return self

    def utc_bounds(self) -> tuple[datetime | None, datetime | None]:
        start = datetime.combine(self.date_from, time.min, BEIJING).astimezone(UTC) if self.date_from else None
        end = (
            datetime.combine(self.date_to + timedelta(days=1), time.min, BEIJING).astimezone(UTC)
            if self.date_to
            else None
        )
        return start, end


class SubmissionPage(BaseModel):
    generated_at: datetime
    total: int
    page: int
    page_size: Literal[25, 50, 100]
    items: list[SubmissionRecord]
