"""后台运行时可热更新的配置模型与密钥脱敏辅助函数；不依赖其他 pdftr 模块。"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from pdftr.domain.url_guard import safe_mask_proxy_url

SecretMode = Literal["custom", "disabled", "inherit_env"]
ConfigSection = Literal["free_translation", "parser", "default_llm", "pricing", "glossary", "system"]

_PROVIDER_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,39}$"
_DEFAULT_FREE_NOTICE = "免费体验将通过 MinerU 解析 PDF 并使用公共推理接口翻译，仅建议上传公开论文。"
_DEFAULT_GLOSSARY_SOURCES = ("aitd", "d2l", "ml-technical-term", "ais-vocab", "computerese")


class SecretView(BaseModel):
    mode: SecretMode = "inherit_env"
    configured: bool = False
    mask: str = ""
    bound_url: str = ""
    bound_protocol: str = ""
    corrupted: bool = False


class SecretUpdate(BaseModel):
    action: Literal["keep", "replace", "disable", "inherit_env"] = "keep"
    value: str = Field(default="", repr=False)


class FreeProviderConfig(BaseModel):
    id: str = Field(pattern=_PROVIDER_ID_PATTERN)
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = True
    protocol: Literal["openai", "anthropic"] = "openai"
    base_url: str
    model: str = Field(min_length=1, max_length=200)
    fallback_model: str = Field(default="", max_length=200)
    timeout_s: int = Field(default=65, ge=15, le=300)


class FreeTranslationConfig(BaseModel):
    enabled: bool = False
    max_pages: int | None = Field(default=None, ge=1, le=2000)
    notice: str = Field(default=_DEFAULT_FREE_NOTICE, max_length=500)
    providers: list[FreeProviderConfig] = Field(default_factory=list, max_length=10)

    @field_validator("providers")
    @classmethod
    def validate_unique_provider_ids(cls, value: list[FreeProviderConfig]) -> list[FreeProviderConfig]:
        seen: set[str] = set()
        for provider in value:
            if provider.id in seen:
                raise ValueError(f"免费翻译渠道 ID 重复：{provider.id}")
            seen.add(provider.id)
        return value


class ParserServiceConfig(BaseModel):
    mineru_base_url: str = "https://mineru.net"
    paddle_public_enabled: bool = False


class LlmPreset(BaseModel):
    id: str
    name: str
    protocol: Literal["openai", "anthropic"] = "openai"
    base_url: str
    models: list[str] = Field(default_factory=list)


class ModelPriceEntry(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    cache_hit: float = Field(ge=0)
    cache_miss: float = Field(ge=0)
    output: float = Field(ge=0)
    peak_factor: float = Field(default=1.0, ge=1.0, le=10.0)

    @field_validator("cache_hit", "cache_miss", "output", "peak_factor")
    @classmethod
    def validate_finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("计价单价必须为有限数字")
        return value


def default_llm_presets() -> list[LlmPreset]:
    return [
        LlmPreset(
            id="deepseek",
            name="DeepSeek",
            protocol="openai",
            base_url="https://api.deepseek.com",
            models=["deepseek-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
        ),
        LlmPreset(
            id="openai",
            name="OpenAI",
            protocol="openai",
            base_url="https://api.openai.com/v1",
            models=["gpt-4o-mini", "gpt-4o"],
        ),
        LlmPreset(
            id="anthropic",
            name="Anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            models=["claude-sonnet-4-5", "claude-haiku-4-5"],
        ),
    ]


def default_model_prices() -> list[ModelPriceEntry]:
    # 与 translation/cost.py 中的内置单价保持一致（元 / 百万 token）
    return [
        ModelPriceEntry(
            model="deepseek-flash",
            cache_hit=0.02,
            cache_miss=1.0,
            output=4.0,
            peak_factor=2.0,
        ),
        ModelPriceEntry(
            model="deepseek-v4-pro",
            cache_hit=0.15,
            cache_miss=4.5,
            output=13.5,
            peak_factor=2.0,
        ),
    ]


class DefaultLlmConfig(BaseModel):
    base_url: str = "https://api.deepseek.com"
    protocol: Literal["openai", "anthropic"] = "openai"
    model: str = "deepseek-flash"
    presets: list[LlmPreset] = Field(default_factory=default_llm_presets)


class PricingConfig(BaseModel):
    models: list[ModelPriceEntry] = Field(default_factory=default_model_prices, max_length=50)
    custom_configured: bool = False

    @model_validator(mode="before")
    @classmethod
    def _infer_legacy_custom_configured(cls, value: Any) -> Any:
        # 兼容 Wave 3 旧格式热价表（未持久化 custom_configured 字段）：含非空 models 即视为已配置热价表
        if isinstance(value, dict) and "custom_configured" not in value:
            models = value.get("models")
            if isinstance(models, list) and len(models) > 0:
                return {**value, "custom_configured": True}
        return value


class GlossaryConfig(BaseModel):
    sources: list[str] = Field(default_factory=lambda: list(_DEFAULT_GLOSSARY_SOURCES))


class SystemConfig(BaseModel):
    max_upload_mb: int = Field(default=200, ge=1, le=2048)
    max_pages: int = Field(default=200, ge=1, le=10000)
    max_concurrent_jobs: int = Field(default=4, ge=1, le=64)
    max_concurrent_translations: int = Field(default=1, ge=1, le=64)
    free_max_concurrent_translations: int = Field(default=1, ge=1, le=32)
    max_queued_jobs: int = Field(default=10, ge=1, le=10000)
    free_max_queued_jobs: int = Field(default=6, ge=1, le=10000)
    retention_hours: int = Field(default=24, ge=0, le=8760)
    min_free_disk_mb: int = Field(default=1024, ge=0)
    maintenance_mode: bool = False
    site_notice: str = Field(default="", max_length=500)


class RuntimeConfigData(BaseModel):
    free_translation: FreeTranslationConfig = Field(default_factory=FreeTranslationConfig)
    parser: ParserServiceConfig = Field(default_factory=ParserServiceConfig)
    default_llm: DefaultLlmConfig = Field(default_factory=DefaultLlmConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    glossary: GlossaryConfig = Field(default_factory=GlossaryConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)


class ConfigRevisionSummary(BaseModel):
    revision: int
    section: str
    changed_fields: list[str]
    summary: str
    created_at: datetime


def mask_secret(value: str) -> str:
    """对密钥做不可逆掩码展示，短密钥全部隐藏，长密钥仅保留极短首尾。"""
    cleaned = value.strip()
    if not cleaned:
        return ""
    if len(cleaned) < 8:
        return "****"
    if len(cleaned) < 12:
        return f"{cleaned[:2]}...{cleaned[-2:]}"
    return f"{cleaned[:4]}...{cleaned[-4:]}"


def mask_proxy_url(url: str) -> str:
    """保留代理 URL 的 scheme 与 host:port，将用户名密码替换为 ***；格式异常返回固定安全掩码。"""
    return safe_mask_proxy_url(url)
