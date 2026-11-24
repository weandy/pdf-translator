"""运行配置：全部来自环境变量（前缀 PDFTR_），见仓库根目录 .env.example。"""

from __future__ import annotations

import base64
import json
import re
from ipaddress import ip_network
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from pdftr.domain.admin import AdminPassword

_CONFIG_SECRET_KEY_ERROR = "PDFTR_CONFIG_SECRET_KEY 必须是 32 字节 URL-safe Base64 密钥"


def decode_config_secret_key(raw: str) -> bytes | None:
    """解码 32 字节（256-bit）URL-safe Base64 配置加密主密钥；空字符串返回 None。"""
    if raw == "":
        return None
    if any(ch.isspace() for ch in raw):
        raise ValueError(_CONFIG_SECRET_KEY_ERROR)
    padded = raw if "=" in raw else raw + "=" * (-len(raw) % 4)
    try:
        key = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(_CONFIG_SECRET_KEY_ERROR) from exc
    if len(key) != 32:
        raise ValueError(_CONFIG_SECRET_KEY_ERROR)
    canonical_padded = base64.urlsafe_b64encode(key).decode("ascii")
    canonical_unpadded = canonical_padded.rstrip("=")
    if raw not in (canonical_padded, canonical_unpadded):
        raise ValueError(_CONFIG_SECRET_KEY_ERROR)
    return key


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PDFTR_", env_file=".env", extra="ignore", hide_input_in_errors=True)

    data_dir: Path = Path("data")
    max_upload_mb: int = Field(default=200, ge=1, le=2048)
    max_pages: int = Field(default=200, ge=1, le=10000)  # MinerU 精准解析单文件上限（S2 / 调研 §4.1）

    # 部署者可选配置的共享 Key；用户在浏览器里填写的 Key 优先（docs/01-tech-stack.md §4.4.2）
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_api_key: str = ""
    llm_protocol: Literal["openai", "anthropic"] = "openai"  # 部署者共享 Key 所用的接口协议
    allow_private_llm_urls: bool = False  # 用户自定义接口是否允许访问内网地址（本地模型部署时显式开启）
    mineru_api_token: str = ""
    # 发往 MinerU（接口、上传、下载结果）的代理，如 socks5h://用户:密码@主机:端口 或 http://主机:端口。
    # 服务器到国内线路差时使用：实测某海外服务器直连上传约 25 KB/s，经洛杉矶一台线路好的机器转发约 1.8 MB/s
    mineru_proxy: str = ""
    mineru_partial_pages_enabled: bool = True
    mineru_sharding_enabled: bool = True
    mineru_shard_threshold: int = Field(default=24, ge=8, le=200)
    mineru_shard_target_pages: int = Field(default=14, ge=6, le=60)
    mineru_shard_min_pages: int = Field(default=8, ge=4, le=30)
    mineru_max_shards: int = Field(default=6, ge=2, le=8)
    layout_cache_ttl_hours: int = Field(default=24, ge=0, le=168)
    paddle_token: str = ""  # 部署者共享的 PaddleOCR（星河社区）Token（可选）
    paddle_proxy: str = ""  # 发往 PaddleOCR 的代理（可选，写法同 PDFTR_MINERU_PROXY）

    llm_concurrency: int = Field(default=0, ge=0, le=64)  # 每个任务同时发给翻译服务的请求数；0 = 按服务商自动
    max_concurrent_jobs: int = Field(default=4, ge=1, le=64)  # 同时上传、解析的任务数
    max_concurrent_translations: int = Field(default=1, ge=1, le=64)  # 同时翻译的任务数
    shutdown_grace_s: float = Field(default=180, gt=0, le=3600)  # 服务停止时等待进行中的任务完成的最长时间
    max_queued_jobs: int = Field(default=10, ge=1, le=10000)  # 排队中的任务超过这个数时拒绝新上传
    retention_hours: int = Field(default=24, ge=0, le=8760)  # 任务文件与译文缓存保留的小时数，0 表示不自动删除
    min_free_disk_mb: int = Field(default=1024, ge=0)  # 磁盘剩余空间低于此值时拒绝新上传
    # 论文术语库来源，逗号分隔，排在前面的优先；首次使用时下载（见 glossary/sources.py）。
    # 机器之心术语库（aitd）为 CC BY-NC-SA 4.0，不可商用：商业部署请去掉 aitd；留空则不用术语库
    glossary_sources: str = "aitd,d2l,ml-technical-term,ais-vocab,computerese"

    font_dir: Path = Path("fonts")  # 导出 PDF 与网页阅读器用的字体（scripts/fetch_fonts.py 生成）
    cors_origins: list[str] = []  # 开发时前端单独运行可填 ["http://localhost:5173"]

    # 明细采集必须由部署者显式开启；管理密码摘要与配置主密钥不出现在配置接口或对象 repr 中。
    submission_audit_enabled: bool = False
    admin_password_hash: str = Field(default="", repr=False)
    config_secret_key: str = Field(default="", repr=False)
    admin_origin: str = ""
    admin_path: str = "/weandy"
    admin_session_days: int = Field(default=30, ge=1, le=30)
    trusted_proxy_cidrs: Annotated[tuple[str, ...], NoDecode] = ()

    @field_validator("admin_path")
    @classmethod
    def validate_admin_path(cls, value: str) -> str:
        reserved = {"admin", "api", "jobs", "stats", "guide", "glossary", "tos", "assets"}
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) or value[1:].lower() in reserved:
            raise ValueError("管理入口必须是未占用的单一路径段")
        return value

    @field_validator("admin_password_hash")
    @classmethod
    def validate_admin_password_hash(cls, value: str) -> str:
        if value:
            AdminPassword.parse(value)
        return value

    @field_validator("config_secret_key")
    @classmethod
    def validate_config_secret_key(cls, value: str) -> str:
        if value:
            decode_config_secret_key(value)
        return value

    @field_validator("admin_origin")
    @classmethod
    def validate_admin_origin(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("管理 Origin 端口无效") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
            or (port is not None and not 1 <= port <= 65535)
            or value != f"https://{parsed.netloc}"
        ):
            raise ValueError("管理 Origin 必须是无路径、无凭据的精确 HTTPS Origin")
        return value

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def validate_trusted_proxy_cidrs(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            value = json.loads(value) if value.strip().startswith("[") else value.split(",")
        if not isinstance(value, (tuple, list)):
            raise ValueError("可信代理 CIDR 必须是逗号分隔文本或数组")
        result = []
        for cidr in value:
            if not isinstance(cidr, str):
                raise ValueError("可信代理 CIDR 必须是字符串")
            if not cidr.strip():
                continue
            network = ip_network(cidr.strip(), strict=True)
            if network.prefixlen == 0:
                raise ValueError("可信代理不得信任全部地址")
            result.append(str(network))
        return tuple(dict.fromkeys(result))

    @model_validator(mode="after")
    def validate_mineru_sharding(self) -> Settings:
        if self.mineru_shard_min_pages > self.mineru_shard_target_pages:
            raise ValueError("MinerU 分片最小页数不能超过目标页数")
        return self

    @model_validator(mode="after")
    def validate_submission_audit(self) -> Settings:
        if self.submission_audit_enabled and not (
            self.admin_password_hash and self.admin_origin and self.trusted_proxy_cidrs
        ):
            raise ValueError("开启提交明细采集前必须配置管理密码哈希、HTTPS Origin 和可信代理 CIDR")
        return self
