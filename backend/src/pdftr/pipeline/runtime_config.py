"""运行时配置热重载编排器：不可变快照、AES-256-GCM 密钥绑定、连接测试与安全回滚。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

import httpx2
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pdftr.config import Settings
from pdftr.domain.llm import ChatError, ChatOptions
from pdftr.domain.runtime_config import (
    ConfigRevisionSummary,
    DefaultLlmConfig,
    FreeProviderConfig,
    FreeTranslationConfig,
    GlossaryConfig,
    LlmPreset,
    ModelPriceEntry,
    ParserServiceConfig,
    PricingConfig,
    PricingSyncConfig,
    PricingSyncStatusView,
    RuntimeConfigData,
    SecretMode,
    SecretUpdate,
    SecretView,
    SystemConfig,
    default_llm_presets,
    mask_proxy_url,
    mask_secret,
)
from pdftr.domain.stats import AdminStatsSnapshot
from pdftr.domain.url_guard import build_safe_async_transport
from pdftr.glossary import sources as glossary_sources
from pdftr.parsing.mineru_cloud import MinerUCloudParser
from pdftr.parsing.paddle_vl import JOB_URL as PADDLE_JOB_URL
from pdftr.parsing.paddle_vl import PaddleVLParser
from pdftr.parsing.port import ParseError
from pdftr.pipeline.pricing_sync import PriceFetcher, PricingSyncService, validate_pricing_source_url
from pdftr.pipeline.secrets import Secrets
from pdftr.pipeline.url_guard import (
    make_outbound_request_hook,
    validate_llm_url,
    validate_parser_url,
    validate_proxy_url,
)
from pdftr.pipeline.usecases import UserError
from pdftr.storage.runtime_config import (
    SecretDecryptError,
    SqliteRuntimeConfigStore,
    StoredSecret,
    decrypt_secret,
    encrypt_secret,
)
from pdftr.translation.anthropic_compat import AnthropicChat
from pdftr.translation.cost import PRICES, Price
from pdftr.translation.openai_compat import MODEL_LIST_TIMEOUT_S, OpenAICompatChat

log = logging.getLogger(__name__)

_MINERU_TOKEN_SLOT = "mineru_token"
_MINERU_PROXY_SLOT = "mineru_proxy"
_PADDLE_TOKEN_SLOT = "paddle_token"
_PADDLE_PROXY_SLOT = "paddle_proxy"
_DEFAULT_MINERU_URL = "https://mineru.net"
_BIND_CONFLICT_MSG = "修改服务地址或协议时必须同时更换或禁用原密钥，避免将旧密钥发送到新地址"
_ENV_MINERU_BIND_CONFLICT_MSG = (
    "修改 MinerU 服务地址后不能继续沿用环境变量中的默认 Token，请更换为目标服务的 Token 或禁用"
)
_NO_FREE_PROVIDER_MSG = "开启免费翻译前请至少启用并配置一个有效上游渠道及密钥"


def _free_slot(provider_id: str) -> str:
    return f"free_provider:{provider_id}"


@dataclass(frozen=True)
class ResolvedSecret:
    mode: SecretMode
    value: str
    mask: str
    bound_url: str
    bound_protocol: str
    corrupted: bool
    source: Literal["custom", "env", "disabled", "corrupted"]

    def __repr__(self) -> str:
        return (
            f"ResolvedSecret(mode={self.mode!r}, source={self.source!r}, "
            f"configured={bool(self.value) and not self.corrupted!r}, corrupted={self.corrupted!r})"
        )

    def to_view(self) -> SecretView:
        return SecretView(
            mode=self.mode,
            configured=bool(self.value) and not self.corrupted,
            mask=self.mask,
            bound_url=self.bound_url,
            bound_protocol=self.bound_protocol,
            corrupted=self.corrupted,
        )


@dataclass(frozen=True)
class ResolvedFreeProvider:
    config: FreeProviderConfig
    api_key: ResolvedSecret
    usable: bool


@dataclass(frozen=True)
class RuntimeSnapshot:
    revision: int
    data: RuntimeConfigData
    free_providers: tuple[ResolvedFreeProvider, ...]
    mineru_token: ResolvedSecret
    mineru_proxy: ResolvedSecret
    paddle_token: ResolvedSecret
    paddle_proxy: ResolvedSecret
    price_overrides: dict[str, Price] | None
    warnings: tuple[str, ...]
    updated_at: datetime | None

    def __repr__(self) -> str:
        return (
            f"RuntimeSnapshot(revision={self.revision}, free_enabled={self.data.free_translation.enabled}, "
            f"usable_providers={len(self.active_free_providers())}, "
            f"mineru_ready={bool(self.mineru_token.value) and not self.mineru_token.corrupted}, "
            f"warnings={len(self.warnings)})"
        )

    def active_free_providers(self) -> list[ResolvedFreeProvider]:
        return [item for item in self.free_providers if item.usable]

    def mineru_ready(self) -> bool:
        return bool(self.mineru_token.value) and not self.mineru_token.corrupted

    def public_paddle_ready(self) -> bool:
        return (
            self.data.parser.paddle_public_enabled and bool(self.paddle_token.value) and not self.paddle_token.corrupted
        )

    def public_parser_ready(self) -> bool:
        return self.mineru_ready() or self.public_paddle_ready()

    def free_channel_ready(self) -> bool:
        return self.data.free_translation.enabled and bool(self.active_free_providers()) and self.public_parser_ready()


# ---- 管理后台 DTO ----


class EnvLockItem(BaseModel):
    name: str
    status: str
    detail: str


class GlossarySourceView(BaseModel):
    id: str
    name: str
    license: str
    commercial_ok: bool
    enabled: bool
    priority: int | None
    cached: bool


class FreeProviderView(BaseModel):
    id: str
    name: str
    enabled: bool
    protocol: Literal["openai", "anthropic"]
    base_url: str
    model: str
    fallback_model: str
    timeout_s: int
    api_key: SecretView
    usable: bool


class FreeTranslationView(BaseModel):
    enabled: bool
    max_pages: int | None
    notice: str
    ready: bool
    providers: list[FreeProviderView]


class ParserServiceView(BaseModel):
    mineru_base_url: str
    mineru_token: SecretView
    mineru_proxy: SecretView
    paddle_public_enabled: bool
    paddle_token: SecretView
    paddle_proxy: SecretView


class GlossaryAdminView(BaseModel):
    sources: list[str]
    available: list[GlossarySourceView]
    loaded_terms: int
    reloading: bool


class AdminConfigView(BaseModel):
    revision: int
    updated_at: datetime | None
    secret_key_ready: bool
    warnings: list[str]
    free_translation: FreeTranslationView
    parser: ParserServiceView
    default_llm: DefaultLlmConfig
    pricing: PricingConfig
    pricing_sync_status: PricingSyncStatusView = Field(default_factory=PricingSyncStatusView)
    glossary: GlossaryAdminView
    system: SystemConfig
    env_locks: list[EnvLockItem]


class FreeProviderPatch(FreeProviderConfig):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretUpdate = Field(default_factory=SecretUpdate)


class FreeTranslationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    enabled: bool = False
    max_pages: int | None = Field(default=None, ge=1, le=2000)
    notice: str = Field(default="", max_length=500)
    providers: list[FreeProviderPatch] = Field(default_factory=list, max_length=10)

    @field_validator("providers")
    @classmethod
    def validate_unique_ids(cls, value: list[FreeProviderPatch]) -> list[FreeProviderPatch]:
        seen: set[str] = set()
        for item in value:
            if item.id in seen:
                raise ValueError(f"免费翻译渠道 ID 重复：{item.id}")
            seen.add(item.id)
        return value


class ParserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    mineru_base_url: str = "https://mineru.net"
    mineru_token: SecretUpdate = Field(default_factory=SecretUpdate)
    mineru_proxy: SecretUpdate = Field(default_factory=SecretUpdate)
    paddle_public_enabled: bool = False
    paddle_token: SecretUpdate = Field(default_factory=SecretUpdate)
    paddle_proxy: SecretUpdate = Field(default_factory=SecretUpdate)


class DefaultLlmPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    base_url: str
    protocol: Literal["openai", "anthropic"] = "openai"
    model: str = Field(min_length=1, max_length=200)
    presets: list[LlmPreset] = Field(default_factory=list, max_length=20)


class PricingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    models: list[ModelPriceEntry] = Field(default_factory=list, max_length=50)
    sync: PricingSyncConfig | None = None


class GlossaryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    sources: list[str] = Field(default_factory=list, max_length=20)


class SystemPatch(SystemConfig):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_revision: int = Field(ge=1)
    expected_revision: int = Field(ge=0)


class ConnectionTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["free_provider", "mineru", "paddle"]
    provider_id: str | None = Field(default=None, max_length=40)
    protocol: Literal["openai", "anthropic"] | None = None
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    timeout_s: int | None = Field(default=None, ge=5, le=300)
    api_key: SecretUpdate | None = None
    token: SecretUpdate | None = None
    proxy: SecretUpdate | None = None


class ConnectionTestOut(BaseModel):
    ok: bool
    status_code: int | None = None
    latency_ms: int = 0
    detail: str = ""


class AdminListModelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["free_provider", "default_llm"]
    provider_id: str | None = Field(default=None, max_length=40)
    protocol: Literal["openai", "anthropic"] = "openai"
    base_url: str = Field(min_length=1, max_length=500)
    api_key: SecretUpdate | None = None
    probe_api_key: str = Field(default="", max_length=500, repr=False)


class AdminListModelsOut(BaseModel):
    ok: bool
    models: list[str] = Field(default_factory=list)
    latency_ms: int = 0
    detail: str = ""
    requires_api_key: bool = False


class QueueSlotStats(BaseModel):
    in_use: int = 0
    limit: int = 0
    waiting: int = 0


class AdminQueuesOut(BaseModel):
    parse: QueueSlotStats
    free: QueueSlotStats
    byok: QueueSlotStats
    parse_reserved_for_byok: bool = False


class AdminServiceSummaryOut(BaseModel):
    free_enabled: bool
    free_ready: bool
    usable_providers: int
    mineru_configured: bool
    paddle_configured: bool
    glossary_terms: int
    glossary_reloading: bool


class ActiveJobSummaryOut(BaseModel):
    job_id: str
    filename: str = ""
    created_at: str | None = None
    page_count: int
    stage: str
    progress: float
    channel_mode: Literal["byok", "free"]
    model: str
    status: str


class AdminOverviewOut(BaseModel):
    queues: AdminQueuesOut
    services: AdminServiceSummaryOut
    warnings: list[str]
    stats: AdminStatsSnapshot
    active_jobs: list[ActiveJobSummaryOut]
    maintenance_mode: bool
    revision: int
    secret_key_ready: bool


class ConfigRevisionsOut(BaseModel):
    items: list[ConfigRevisionSummary]
    limit: int
    offset: int


ChatTester = Callable[[str, str, str, str, int], Awaitable[ConnectionTestOut]]
ParserTester = Callable[[Literal["mineru", "paddle"], str, str, str], Awaitable[tuple[int, int]]]
ModelLister = Callable[[str, str, str], Awaitable[list[str]]]


def _baseline_from_settings(settings: Settings) -> RuntimeConfigData:
    return RuntimeConfigData(
        free_translation=FreeTranslationConfig(),
        parser=ParserServiceConfig(),
        default_llm=DefaultLlmConfig(
            base_url=settings.llm_base_url.rstrip("/"),
            protocol=settings.llm_protocol,
            model=settings.llm_model,
            presets=default_llm_presets(),
        ),
        pricing=PricingConfig(),
        glossary=GlossaryConfig(sources=[src.id for src in glossary_sources.parse_ids(settings.glossary_sources)]),
        system=SystemConfig(
            max_upload_mb=settings.max_upload_mb,
            max_pages=settings.max_pages,
            max_concurrent_jobs=settings.max_concurrent_jobs,
            max_concurrent_translations=settings.max_concurrent_translations,
            free_max_concurrent_translations=1,
            max_queued_jobs=settings.max_queued_jobs,
            free_max_queued_jobs=6,
            retention_hours=settings.retention_hours,
            min_free_disk_mb=settings.min_free_disk_mb,
            maintenance_mode=False,
            site_notice="",
        ),
    )


def _prices_from_config(pricing: PricingConfig) -> dict[str, Price]:
    return {
        entry.model: Price(
            cache_hit=entry.cache_hit,
            cache_miss=entry.cache_miss,
            output=entry.output,
            peak_factor=entry.peak_factor,
        )
        for entry in pricing.models
    }


def _env_secret(
    value: str,
    *,
    is_proxy: bool,
    bound_url: str = "",
    bound_protocol: str = "",
) -> ResolvedSecret:
    cleaned = value.strip()
    mask = mask_proxy_url(cleaned) if is_proxy else mask_secret(cleaned)
    return ResolvedSecret(
        mode="inherit_env",
        value=cleaned,
        mask=mask,
        bound_url=bound_url,
        bound_protocol=bound_protocol,
        corrupted=False,
        source="env",
    )


class RuntimeConfigManager:
    """单进程运行时配置管理器：持锁落库，原子替换只读内存快照。"""

    def __init__(
        self,
        *,
        settings: Settings,
        store: SqliteRuntimeConfigStore,
        secret_key: bytes | None,
        secret_key_source: Literal["env", "file", "unavailable"] | None = None,
        secret_key_warning: str = "",
        on_limits_changed: Callable[[SystemConfig, int], None] | None = None,
        on_glossary_changed: Callable[[tuple[str, ...], int], None] | None = None,
        on_glossary_reload: Callable[[tuple[str, ...], int], None] | None = None,
        glossary_status: Callable[[], tuple[int, bool]] | None = None,
        glossary_cached: Callable[[str], bool] | None = None,
        chat_tester: ChatTester | None = None,
        parser_tester: ParserTester | None = None,
        model_lister: ModelLister | None = None,
        price_fetcher: PriceFetcher | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._secret_key = secret_key
        self._secret_key_source: Literal["env", "file", "unavailable"] = (
            secret_key_source if secret_key_source is not None else ("env" if secret_key is not None else "unavailable")
        )
        self._secret_key_warning = secret_key_warning.strip()
        self._on_limits_changed = on_limits_changed
        self._on_glossary_changed = on_glossary_changed
        self._on_glossary_reload = on_glossary_reload
        self._glossary_status = glossary_status
        self._glossary_cached = glossary_cached
        self._chat_tester = chat_tester
        self._parser_tester = parser_tester
        self._model_lister = model_lister
        self._pricing_sync = PricingSyncService(
            settings.data_dir / "pricing" / "remote_prices.json",
            allow_private=settings.allow_private_llm_urls,
            fetcher=price_fetcher,
        )
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._secrets_corrupted = False

        stored = store.load()
        if stored is None:
            self._raw_secrets: dict[str, StoredSecret] = {}
            self._snapshot = self._build_snapshot(0, _baseline_from_settings(settings), {}, None)
        elif stored.secrets_corrupted:
            # 整表损坏时将全部站点槽位显式锁定为 disabled+corrupted，绝不回退复活 .env 凭据
            self._secrets_corrupted = True
            locked_slots: dict[str, StoredSecret] = {
                _MINERU_TOKEN_SLOT: StoredSecret(mode="disabled", corrupted=True),
                _MINERU_PROXY_SLOT: StoredSecret(mode="disabled", corrupted=True),
                _PADDLE_TOKEN_SLOT: StoredSecret(mode="disabled", corrupted=True),
                _PADDLE_PROXY_SLOT: StoredSecret(mode="disabled", corrupted=True),
            }
            for provider in stored.data.free_translation.providers:
                locked_slots[_free_slot(provider.id)] = StoredSecret(mode="disabled", corrupted=True)
            self._raw_secrets = locked_slots
            self._snapshot = self._build_snapshot(
                stored.revision,
                stored.data,
                self._raw_secrets,
                stored.updated_at,
                secrets_corrupted=True,
            )
        else:
            self._raw_secrets = {k: replace(v) for k, v in stored.secrets.items()}
            self._snapshot = self._build_snapshot(stored.revision, stored.data, self._raw_secrets, stored.updated_at)

    @property
    def secret_key_ready(self) -> bool:
        return self._secret_key is not None

    def set_callbacks(
        self,
        *,
        on_limits_changed: Callable[[SystemConfig, int], None] | None = None,
        on_glossary_changed: Callable[[tuple[str, ...], int], None] | None = None,
        on_glossary_reload: Callable[[tuple[str, ...], int], None] | None = None,
    ) -> None:
        if on_limits_changed is not None:
            self._on_limits_changed = on_limits_changed
        if on_glossary_changed is not None:
            self._on_glossary_changed = on_glossary_changed
        if on_glossary_reload is not None:
            self._on_glossary_reload = on_glossary_reload

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def close(self) -> None:
        self._store.close()

    def snapshot(self) -> RuntimeSnapshot:
        """无锁零 IO 读取当前不可变快照；若继承 .env 的槽位在进程内变更（如测试），实时反映。"""
        snap = self._snapshot
        s = self._settings
        changed = False
        mineru_token = snap.mineru_token
        if mineru_token.mode == "inherit_env":
            env_mineru = s.mineru_api_token.strip()
            mineru_url = snap.data.parser.mineru_base_url.rstrip("/")
            if env_mineru and mineru_url != _DEFAULT_MINERU_URL:
                if mineru_token.value != "" or not mineru_token.corrupted:
                    mineru_token = ResolvedSecret(
                        mode="inherit_env",
                        value="",
                        mask=mask_secret(env_mineru),
                        bound_url=_DEFAULT_MINERU_URL,
                        bound_protocol="mineru",
                        corrupted=True,
                        source="corrupted",
                    )
                    changed = True
            elif mineru_token.corrupted or mineru_token.value != env_mineru:
                mineru_token = _env_secret(
                    s.mineru_api_token,
                    is_proxy=False,
                    bound_url=_DEFAULT_MINERU_URL,
                    bound_protocol="mineru",
                )
                changed = True
        mineru_proxy = snap.mineru_proxy
        if mineru_proxy.mode == "inherit_env" and mineru_proxy.value != s.mineru_proxy.strip():
            mineru_proxy = _env_secret(s.mineru_proxy, is_proxy=True)
            changed = True
        paddle_token = snap.paddle_token
        if paddle_token.mode == "inherit_env" and paddle_token.value != s.paddle_token.strip():
            paddle_token = _env_secret(s.paddle_token, is_proxy=False)
            changed = True
        paddle_proxy = snap.paddle_proxy
        if paddle_proxy.mode == "inherit_env" and paddle_proxy.value != s.paddle_proxy.strip():
            paddle_proxy = _env_secret(s.paddle_proxy, is_proxy=True)
            changed = True
        if not changed:
            return snap
        return replace(
            snap,
            mineru_token=mineru_token,
            mineru_proxy=mineru_proxy,
            paddle_token=paddle_token,
            paddle_proxy=paddle_proxy,
        )

    def _resolve_slot(
        self,
        slot: str,
        label: str,
        raw: StoredSecret | None,
        *,
        env_value: str,
        is_proxy: bool,
        allow_inherit_env: bool,
        warnings: list[str],
        env_bound_url: str = "",
        env_bound_protocol: str = "",
    ) -> ResolvedSecret:
        if raw is None:
            if allow_inherit_env:
                return _env_secret(
                    env_value,
                    is_proxy=is_proxy,
                    bound_url=env_bound_url,
                    bound_protocol=env_bound_protocol,
                )
            return ResolvedSecret("disabled", "", "", "", "", False, "disabled")
        if raw.corrupted:
            warnings.append(f"「{label}」存储元数据损坏，已降级停用，请重新填写保存")
            return ResolvedSecret("disabled", "", "", "", "", True, "corrupted")
        if raw.mode == "disabled":
            return ResolvedSecret("disabled", "", "", "", "", False, "disabled")
        if raw.mode == "inherit_env":
            if allow_inherit_env:
                return _env_secret(
                    env_value,
                    is_proxy=is_proxy,
                    bound_url=env_bound_url,
                    bound_protocol=env_bound_protocol,
                )
            return ResolvedSecret("disabled", "", "", "", "", False, "disabled")
        # mode == "custom"
        if not raw.enc:
            warnings.append(f"「{label}」密文缺失，已降级停用，请重新填写保存")
            return ResolvedSecret("custom", "", raw.mask, raw.bound_url, raw.bound_protocol, True, "corrupted")
        if self._secret_key is None:
            warnings.append(f"未配置 PDFTR_CONFIG_SECRET_KEY，无法解密「{label}」，对应能力已降级停用")
            return ResolvedSecret("custom", "", raw.mask, raw.bound_url, raw.bound_protocol, True, "corrupted")
        try:
            plain = decrypt_secret(self._secret_key, slot, raw.enc)
        except SecretDecryptError:
            warnings.append(f"「{label}」密文解密失败（主密钥可能已更换或数据损坏），已降级停用，请重新填写保存")
            return ResolvedSecret("custom", "", raw.mask, raw.bound_url, raw.bound_protocol, True, "corrupted")
        mask = raw.mask or (mask_proxy_url(plain) if is_proxy else mask_secret(plain))
        return ResolvedSecret("custom", plain, mask, raw.bound_url, raw.bound_protocol, False, "custom")

    def _build_snapshot(
        self,
        revision: int,
        data: RuntimeConfigData,
        secrets: dict[str, StoredSecret],
        updated_at: datetime | None,
        *,
        secrets_corrupted: bool = False,
    ) -> RuntimeSnapshot:
        warnings: list[str] = []
        if self._secret_key_warning:
            warnings.append(self._secret_key_warning)
        if secrets_corrupted:
            warnings.append("运行时密钥存储表已损坏，全部站点凭据与代理已降级停用（不会回退环境变量），请重新填写保存")
        s = self._settings
        mineru_token = self._resolve_slot(
            _MINERU_TOKEN_SLOT,
            "MinerU Token",
            secrets.get(_MINERU_TOKEN_SLOT),
            env_value=s.mineru_api_token,
            is_proxy=False,
            allow_inherit_env=True,
            warnings=warnings,
            env_bound_url=_DEFAULT_MINERU_URL,
            env_bound_protocol="mineru",
        )
        mineru_proxy = self._resolve_slot(
            _MINERU_PROXY_SLOT,
            "MinerU 代理",
            secrets.get(_MINERU_PROXY_SLOT),
            env_value=s.mineru_proxy,
            is_proxy=True,
            allow_inherit_env=True,
            warnings=warnings,
        )
        paddle_token = self._resolve_slot(
            _PADDLE_TOKEN_SLOT,
            "PaddleOCR Token",
            secrets.get(_PADDLE_TOKEN_SLOT),
            env_value=s.paddle_token,
            is_proxy=False,
            allow_inherit_env=True,
            warnings=warnings,
        )
        paddle_proxy = self._resolve_slot(
            _PADDLE_PROXY_SLOT,
            "PaddleOCR 代理",
            secrets.get(_PADDLE_PROXY_SLOT),
            env_value=s.paddle_proxy,
            is_proxy=True,
            allow_inherit_env=True,
            warnings=warnings,
        )
        norm_mineru_url = data.parser.mineru_base_url.rstrip("/")
        if (
            mineru_token.mode == "custom"
            and not mineru_token.corrupted
            and mineru_token.bound_url
            and mineru_token.bound_url != norm_mineru_url
        ):
            warnings.append("MinerU 自定义 Token 与当前服务地址绑定不一致，已降级停用，请重新填写保存")
            mineru_token = replace(mineru_token, value="", corrupted=True, source="corrupted")
        elif (
            mineru_token.mode == "inherit_env"
            and not mineru_token.corrupted
            and mineru_token.value
            and norm_mineru_url != _DEFAULT_MINERU_URL
        ):
            warnings.append(
                "MinerU 服务地址已偏离官方默认地址，已停用环境变量默认 Token，请更换目标服务的 Token 或禁用"
            )
            mineru_token = replace(mineru_token, value="", corrupted=True, source="corrupted")

        resolved_providers: list[ResolvedFreeProvider] = []
        for cfg in data.free_translation.providers:
            sec = self._resolve_slot(
                _free_slot(cfg.id),
                f"免费渠道 {cfg.name}({cfg.id}) 密钥",
                secrets.get(_free_slot(cfg.id)),
                env_value="",
                is_proxy=False,
                allow_inherit_env=False,
                warnings=warnings,
            )
            bound_ok = True
            if sec.mode == "custom":
                bound_ok = sec.bound_url == cfg.base_url.rstrip("/") and sec.bound_protocol == cfg.protocol
                if not bound_ok and not sec.corrupted:
                    warnings.append(f"免费渠道「{cfg.name}」的密钥与地址或协议绑定不一致，已停用")
            usable = bool(cfg.enabled and sec.value and not sec.corrupted and bound_ok)
            resolved_providers.append(ResolvedFreeProvider(config=cfg, api_key=sec, usable=usable))

        snap = RuntimeSnapshot(
            revision=revision,
            data=data.model_copy(deep=True),
            free_providers=tuple(resolved_providers),
            mineru_token=mineru_token,
            mineru_proxy=mineru_proxy,
            paddle_token=paddle_token,
            paddle_proxy=paddle_proxy,
            price_overrides=self._effective_price_overrides(data.pricing),
            warnings=tuple(dict.fromkeys(warnings)),
            updated_at=updated_at,
        )
        if data.free_translation.enabled and not snap.free_channel_ready():
            extra = (
                "免费体验通道已开启，但 MinerU 与公开 PaddleOCR 解析服务均未就绪，前台免费入口暂不可用"
                if snap.active_free_providers()
                else "免费体验通道已开启，但当前无可用上游渠道或密钥，前台免费入口已降级隐藏"
            )
            snap = replace(snap, warnings=(*snap.warnings, extra))
        return snap

    def _apply_secret_update(
        self,
        *,
        slot: str,
        label: str,
        update: SecretUpdate,
        old: StoredSecret | None,
        target_url: str,
        target_protocol: str,
        is_proxy: bool,
        allow_inherit_env: bool,
        env_value: str = "",
        env_bound_url: str = "",
    ) -> tuple[StoredSecret, str | None]:
        """应用单槽密钥三态更新，返回 (新 StoredSecret, 脱敏动作描述或 None)。"""
        norm_url = target_url.rstrip("/") if target_url else ""
        env_bound_conflict = bool(env_bound_url and env_value.strip() and norm_url and norm_url != env_bound_url)
        if update.action == "keep":
            if (
                old is not None
                and old.mode == "custom"
                and not old.corrupted
                and norm_url
                and (old.bound_url != norm_url or (target_protocol and old.bound_protocol != target_protocol))
            ):
                raise UserError(_BIND_CONFLICT_MSG)
            if env_bound_conflict and (old is None or (old.mode == "inherit_env" and not old.corrupted)):
                raise UserError(_ENV_MINERU_BIND_CONFLICT_MSG)
            if old is not None:
                return replace(old), None
            return StoredSecret(mode="inherit_env" if allow_inherit_env else "disabled"), None
        if update.action == "disable":
            changed = old is None or old.mode != "disabled" or old.corrupted
            return StoredSecret(mode="disabled"), "disabled" if changed else None
        if update.action == "inherit_env":
            if not allow_inherit_env:
                raise UserError(f"{label}不支持继承环境变量，请选择更换或禁用")
            if env_bound_conflict:
                raise UserError(_ENV_MINERU_BIND_CONFLICT_MSG)
            changed = old is None or old.mode != "inherit_env" or old.corrupted
            return StoredSecret(mode="inherit_env"), "inherit_env" if changed else None
        # action == "replace"
        cleaned = update.value.strip()
        if not cleaned:
            raise UserError(f"请填写新的{label}")
        if is_proxy:
            try:
                cleaned = validate_proxy_url(cleaned, allow_private=self._settings.allow_private_llm_urls)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
        if self._secret_key is None:
            raise UserError(
                "加密主密钥未就绪，禁止保存自定义密钥或代理；"
                "请检查数据目录写权限或配置 PDFTR_CONFIG_SECRET_KEY 后重启服务"
            )
        enc = encrypt_secret(self._secret_key, slot, cleaned)
        mask = mask_proxy_url(cleaned) if is_proxy else mask_secret(cleaned)
        return (
            StoredSecret(
                mode="custom",
                enc=enc,
                mask=mask,
                bound_url=norm_url,
                bound_protocol=target_protocol,
            ),
            "replaced",
        )

    def _notify_limits(self, system_cfg: SystemConfig, revision: int) -> None:
        if self._on_limits_changed is None:
            return
        cb = self._on_limits_changed
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(cb, system_cfg, revision)
        else:
            cb(system_cfg, revision)

    def _notify_glossary(self, sources: list[str] | tuple[str, ...], revision: int, *, force: bool = False) -> None:
        items = tuple(sources)
        if force and self._on_glossary_reload is not None:
            self._on_glossary_reload(items, revision)
            return
        if self._on_glossary_changed is None:
            return
        self._on_glossary_changed(items, revision)

    def _commit_locked(
        self,
        *,
        expected_revision: int,
        new_data: RuntimeConfigData,
        new_secrets: dict[str, StoredSecret],
        section: str,
        changed_fields: list[str],
        summary: str,
    ) -> RuntimeSnapshot:
        saved = self._store.save(
            expected_revision,
            new_data,
            new_secrets,
            section=section,
            changed_fields=changed_fields,
            summary=summary,
        )
        self._secrets_corrupted = False
        self._raw_secrets = {k: replace(v) for k, v in saved.secrets.items()}
        self._snapshot = self._build_snapshot(saved.revision, saved.data, self._raw_secrets, saved.updated_at)
        return self._snapshot

    def _env_locks(self) -> list[EnvLockItem]:
        s = self._settings
        if self._secret_key_source == "env" and self.secret_key_ready:
            key_status = "已配置（.env）"
            key_detail = "通过环境变量 PDFTR_CONFIG_SECRET_KEY 注入，用于 AES-256-GCM 加密后台保存的密钥与代理"
        elif self._secret_key_source == "file" and self.secret_key_ready:
            key_status = "自动生成（本地文件）"
            key_detail = "已在数据目录自动生成并持久化本地主密钥（.config_secret.key），也可通过 .env 覆盖"
        else:
            key_status = "未就绪"
            key_detail = self._secret_key_warning or "加密主密钥未就绪，暂时无法在后台保存自定义密钥或代理"
        return [
            EnvLockItem(
                name="PDFTR_CONFIG_SECRET_KEY",
                status=key_status,
                detail=key_detail,
            ),
            EnvLockItem(
                name="PDFTR_ADMIN_ORIGIN",
                status="已锁定" if s.admin_origin else "未配置",
                detail=s.admin_origin or "管理后台精确 HTTPS Origin，仅支持启动期修改",
            ),
            EnvLockItem(
                name="PDFTR_ADMIN_PATH",
                status="已锁定",
                detail=f"当前管理入口路径：{s.admin_path}（修改需重启服务）",
            ),
            EnvLockItem(
                name="PDFTR_SUBMISSION_AUDIT_ENABLED",
                status="已开启" if s.submission_audit_enabled else "已关闭",
                detail="提交明细采集开关属于合规边界，仅允许通过环境变量调整",
            ),
            EnvLockItem(
                name="PDFTR_ALLOW_PRIVATE_LLM_URLS",
                status="允许内网" if s.allow_private_llm_urls else "仅公网",
                detail="SSRF 出站地址安全边界，仅允许通过环境变量控制",
            ),
        ]

    def _glossary_view(self, snap: RuntimeSnapshot) -> GlossaryAdminView:
        loaded_terms, reloading = self._glossary_status() if self._glossary_status else (0, False)
        enabled_list = list(snap.data.glossary.sources)
        priority_map = {sid: idx + 1 for idx, sid in enumerate(enabled_list)}
        vendor_dir = self._settings.data_dir / "glossaries" / "vendor"
        available: list[GlossarySourceView] = []
        for sid, src in glossary_sources.SOURCES.items():
            cached = (
                self._glossary_cached(sid)
                if self._glossary_cached is not None
                else (vendor_dir / src.cache_name).exists()
            )
            available.append(
                GlossarySourceView(
                    id=src.id,
                    name=src.name,
                    license=src.license,
                    commercial_ok="不可商用" not in src.license and "NC" not in src.license,
                    enabled=sid in priority_map,
                    priority=priority_map.get(sid),
                    cached=cached,
                )
            )
        return GlossaryAdminView(
            sources=enabled_list,
            available=available,
            loaded_terms=loaded_terms,
            reloading=reloading,
        )

    def admin_view(self) -> AdminConfigView:
        snap = self.snapshot()
        return AdminConfigView(
            revision=snap.revision,
            updated_at=snap.updated_at,
            secret_key_ready=self.secret_key_ready,
            warnings=list(snap.warnings),
            free_translation=FreeTranslationView(
                enabled=snap.data.free_translation.enabled,
                max_pages=snap.data.free_translation.max_pages,
                notice=snap.data.free_translation.notice,
                ready=snap.free_channel_ready(),
                providers=[
                    FreeProviderView(
                        id=p.config.id,
                        name=p.config.name,
                        enabled=p.config.enabled,
                        protocol=p.config.protocol,
                        base_url=p.config.base_url,
                        model=p.config.model,
                        fallback_model=p.config.fallback_model,
                        timeout_s=p.config.timeout_s,
                        api_key=p.api_key.to_view(),
                        usable=p.usable,
                    )
                    for p in snap.free_providers
                ],
            ),
            parser=ParserServiceView(
                mineru_base_url=snap.data.parser.mineru_base_url,
                mineru_token=snap.mineru_token.to_view(),
                mineru_proxy=snap.mineru_proxy.to_view(),
                paddle_public_enabled=snap.data.parser.paddle_public_enabled,
                paddle_token=snap.paddle_token.to_view(),
                paddle_proxy=snap.paddle_proxy.to_view(),
            ),
            default_llm=snap.data.default_llm,
            pricing=snap.data.pricing,
            pricing_sync_status=self._pricing_sync.status_view(snap.data.pricing.sync.source_url),
            glossary=self._glossary_view(snap),
            system=snap.data.system,
            env_locks=self._env_locks(),
        )

    def update_free_translation(self, patch: FreeTranslationPatch) -> AdminConfigView:
        allow_private = self._settings.allow_private_llm_urls
        with self._lock:
            old_data = self._snapshot.data
            new_secrets = {k: replace(v) for k, v in self._raw_secrets.items() if not k.startswith("free_provider:")}
            new_providers: list[FreeProviderConfig] = []
            changed_fields: list[str] = []

            for item in patch.providers:
                try:
                    norm_url = validate_llm_url(
                        item.base_url,
                        allow_private=allow_private,
                        require_https=not allow_private,
                    )
                except ValueError as exc:
                    raise UserError(f"免费渠道「{item.name}」：{exc}") from exc
                model_name = item.model.strip()
                if not model_name:
                    raise UserError(f"免费渠道「{item.name}」的模型名不能为空")
                slot = _free_slot(item.id)
                stored_secret, action_note = self._apply_secret_update(
                    slot=slot,
                    label=f"免费渠道「{item.name}」API Key",
                    update=item.api_key,
                    old=self._raw_secrets.get(slot),
                    target_url=norm_url,
                    target_protocol=item.protocol,
                    is_proxy=False,
                    allow_inherit_env=False,
                )
                new_secrets[slot] = stored_secret
                if action_note:
                    changed_fields.append(f"free_provider:{item.id}.api_key({action_note})")
                new_providers.append(
                    FreeProviderConfig(
                        id=item.id,
                        name=item.name.strip(),
                        enabled=item.enabled,
                        protocol=item.protocol,
                        base_url=norm_url,
                        model=model_name,
                        fallback_model=item.fallback_model.strip(),
                        timeout_s=item.timeout_s,
                    )
                )

            new_free = FreeTranslationConfig(
                enabled=patch.enabled,
                max_pages=patch.max_pages,
                notice=patch.notice.strip(),
                providers=new_providers,
            )
            new_data = old_data.model_copy(update={"free_translation": new_free})
            candidate = self._build_snapshot(self._snapshot.revision + 1, new_data, new_secrets, None)
            if new_free.enabled and not candidate.active_free_providers():
                raise UserError(_NO_FREE_PROVIDER_MSG)

            if old_data.free_translation.enabled != new_free.enabled:
                changed_fields.append("free_translation.enabled")
            if old_data.free_translation.max_pages != new_free.max_pages:
                changed_fields.append("free_translation.max_pages")
            if old_data.free_translation.notice != new_free.notice:
                changed_fields.append("free_translation.notice")
            if old_data.free_translation.providers != new_free.providers:
                changed_fields.append("free_translation.providers")

            summary = (
                f"更新免费翻译配置（状态：{'启用' if new_free.enabled else '停用'}，渠道数：{len(new_providers)}）"
            )
            self._commit_locked(
                expected_revision=patch.expected_revision,
                new_data=new_data,
                new_secrets=new_secrets,
                section="free_translation",
                changed_fields=changed_fields or ["free_translation"],
                summary=summary,
            )
        return self.admin_view()

    def update_parser(self, patch: ParserPatch) -> AdminConfigView:
        allow_private = self._settings.allow_private_llm_urls
        try:
            norm_mineru_url = validate_parser_url(patch.mineru_base_url, allow_private=allow_private)
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        with self._lock:
            old_data = self._snapshot.data
            new_secrets = {k: replace(v) for k, v in self._raw_secrets.items()}
            changed_fields: list[str] = []

            mineru_tok, note = self._apply_secret_update(
                slot=_MINERU_TOKEN_SLOT,
                label="MinerU Token",
                update=patch.mineru_token,
                old=self._raw_secrets.get(_MINERU_TOKEN_SLOT),
                target_url=norm_mineru_url,
                target_protocol="mineru",
                is_proxy=False,
                allow_inherit_env=True,
                env_value=self._settings.mineru_api_token,
                env_bound_url=_DEFAULT_MINERU_URL,
            )
            new_secrets[_MINERU_TOKEN_SLOT] = mineru_tok
            if note:
                changed_fields.append(f"parser.mineru_token({note})")

            mineru_prx, note = self._apply_secret_update(
                slot=_MINERU_PROXY_SLOT,
                label="MinerU 代理",
                update=patch.mineru_proxy,
                old=self._raw_secrets.get(_MINERU_PROXY_SLOT),
                target_url="",
                target_protocol="",
                is_proxy=True,
                allow_inherit_env=True,
            )
            new_secrets[_MINERU_PROXY_SLOT] = mineru_prx
            if note:
                changed_fields.append(f"parser.mineru_proxy({note})")

            paddle_tok, note = self._apply_secret_update(
                slot=_PADDLE_TOKEN_SLOT,
                label="PaddleOCR Token",
                update=patch.paddle_token,
                old=self._raw_secrets.get(_PADDLE_TOKEN_SLOT),
                target_url=PADDLE_JOB_URL,
                target_protocol="paddle",
                is_proxy=False,
                allow_inherit_env=True,
            )
            new_secrets[_PADDLE_TOKEN_SLOT] = paddle_tok
            if note:
                changed_fields.append(f"parser.paddle_token({note})")

            paddle_prx, note = self._apply_secret_update(
                slot=_PADDLE_PROXY_SLOT,
                label="PaddleOCR 代理",
                update=patch.paddle_proxy,
                old=self._raw_secrets.get(_PADDLE_PROXY_SLOT),
                target_url="",
                target_protocol="",
                is_proxy=True,
                allow_inherit_env=True,
            )
            new_secrets[_PADDLE_PROXY_SLOT] = paddle_prx
            if note:
                changed_fields.append(f"parser.paddle_proxy({note})")

            new_parser = ParserServiceConfig(
                mineru_base_url=norm_mineru_url,
                paddle_public_enabled=patch.paddle_public_enabled,
            )
            if old_data.parser.mineru_base_url != new_parser.mineru_base_url:
                changed_fields.append("parser.mineru_base_url")
            if old_data.parser.paddle_public_enabled != new_parser.paddle_public_enabled:
                changed_fields.append("parser.paddle_public_enabled")

            new_data = old_data.model_copy(update={"parser": new_parser})
            self._commit_locked(
                expected_revision=patch.expected_revision,
                new_data=new_data,
                new_secrets=new_secrets,
                section="parser",
                changed_fields=changed_fields or ["parser"],
                summary="更新文档解析服务与代理配置",
            )
        return self.admin_view()

    def update_default_llm(self, patch: DefaultLlmPatch) -> AdminConfigView:
        allow_private = self._settings.allow_private_llm_urls
        try:
            norm_url = validate_llm_url(patch.base_url, allow_private=allow_private, require_https=not allow_private)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        model_name = patch.model.strip()
        if not model_name:
            raise UserError("默认模型名不能为空")

        presets: list[LlmPreset] = []
        seen_ids: set[str] = set()
        for preset in patch.presets:
            pid = preset.id.strip()
            if not pid or pid in seen_ids:
                raise UserError(f"预设 ID 无效或重复：{preset.id}")
            seen_ids.add(pid)
            try:
                preset_url = validate_llm_url(
                    preset.base_url, allow_private=allow_private, require_https=not allow_private
                )
            except ValueError as exc:
                raise UserError(f"预设「{preset.name}」：{exc}") from exc
            models = [m.strip() for m in preset.models if m.strip()]
            presets.append(
                LlmPreset(
                    id=pid,
                    name=preset.name.strip() or pid,
                    protocol=preset.protocol,
                    base_url=preset_url,
                    models=models,
                )
            )

        new_default = DefaultLlmConfig(
            base_url=norm_url,
            protocol=patch.protocol,
            model=model_name,
            presets=presets,
        )
        with self._lock:
            old_data = self._snapshot.data
            changed_fields: list[str] = []
            if old_data.default_llm.base_url != new_default.base_url:
                changed_fields.append("default_llm.base_url")
            if old_data.default_llm.protocol != new_default.protocol:
                changed_fields.append("default_llm.protocol")
            if old_data.default_llm.model != new_default.model:
                changed_fields.append("default_llm.model")
            if old_data.default_llm.presets != new_default.presets:
                changed_fields.append("default_llm.presets")
            new_data = old_data.model_copy(update={"default_llm": new_default})
            self._commit_locked(
                expected_revision=patch.expected_revision,
                new_data=new_data,
                new_secrets=self._raw_secrets,
                section="default_llm",
                changed_fields=changed_fields or ["default_llm"],
                summary=f"更新默认翻译接口与快捷预设（模型：{model_name}）",
            )
        return self.admin_view()

    def _effective_price_overrides(self, pricing: PricingConfig) -> dict[str, Price] | None:
        """双层价表合并：手动热表（或未自定义时的内置 DeepSeek 价）始终优先于远程同步缓存。"""
        remote_prices = (
            self._pricing_sync.prices_cny(pricing.sync.usd_to_cny) if pricing.sync.source_url.strip() else {}
        )
        if not remote_prices:
            return _prices_from_config(pricing) if pricing.custom_configured else None
        merged: dict[str, Price] = dict(remote_prices)
        if pricing.custom_configured:
            merged.update(_prices_from_config(pricing))
        else:
            merged.update(PRICES)
        return merged

    def update_pricing(self, patch: PricingPatch) -> AdminConfigView:
        cleaned: list[ModelPriceEntry] = []
        seen: set[str] = set()
        for entry in patch.models:
            name = entry.model.strip()
            if not name:
                raise UserError("计价模型名不能为空")
            if name in seen:
                raise UserError(f"模型单价重复：{name}")
            seen.add(name)
            cleaned.append(entry.model_copy(update={"model": name}))

        allow_private = self._settings.allow_private_llm_urls
        new_sync: PricingSyncConfig | None = None
        if patch.sync is not None:
            try:
                norm_source_url = validate_pricing_source_url(patch.sync.source_url, allow_private=allow_private)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
            new_sync = patch.sync.model_copy(update={"source_url": norm_source_url})

        with self._lock:
            old_pricing = self._snapshot.data.pricing
            effective_sync = new_sync if new_sync is not None else old_pricing.sync
            new_pricing = PricingConfig(models=cleaned, custom_configured=True, sync=effective_sync)
            changed_fields: list[str] = []
            if old_pricing.models != new_pricing.models:
                changed_fields.append("pricing.models")
            if old_pricing.sync != new_pricing.sync:
                changed_fields.append("pricing.sync")
            new_data = self._snapshot.data.model_copy(update={"pricing": new_pricing})
            self._commit_locked(
                expected_revision=patch.expected_revision,
                new_data=new_data,
                new_secrets=self._raw_secrets,
                section="pricing",
                changed_fields=changed_fields or ["pricing.models"],
                summary=f"更新模型计价与同步配置（手动热表 {len(cleaned)} 个模型）",
            )
        return self.admin_view()

    async def sync_pricing(self, *, force: bool = False) -> PricingSyncStatusView:
        snap = self.snapshot()
        try:
            status = await self._pricing_sync.sync_now(snap.data.pricing.sync, force=force)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        with self._lock:
            cur = self._snapshot
            self._snapshot = replace(cur, price_overrides=self._effective_price_overrides(cur.data.pricing))
        return status

    async def sync_pricing_if_due(self) -> bool:
        snap = self.snapshot()
        sync_cfg = snap.data.pricing.sync
        if not self._pricing_sync.is_due(sync_cfg):
            return False
        await self.sync_pricing(force=False)
        return True

    def update_glossary(self, patch: GlossaryPatch) -> AdminConfigView:
        ordered: list[str] = []
        for raw_sid in patch.sources:
            sid = raw_sid.strip()
            if not sid:
                continue
            if sid not in glossary_sources.SOURCES:
                raise UserError(f"未知的术语库来源：{sid}")
            if sid not in ordered:
                ordered.append(sid)

        with self._lock:
            new_data = self._snapshot.data.model_copy(update={"glossary": GlossaryConfig(sources=ordered)})
            saved = self._commit_locked(
                expected_revision=patch.expected_revision,
                new_data=new_data,
                new_secrets=self._raw_secrets,
                section="glossary",
                changed_fields=["glossary.sources"],
                summary=f"更新术语库来源（共 {len(ordered)} 个）",
            )
        self._notify_glossary(ordered, saved.revision)
        return self.admin_view()

    def update_system(self, patch: SystemPatch) -> AdminConfigView:
        new_system = SystemConfig(
            max_upload_mb=patch.max_upload_mb,
            max_pages=patch.max_pages,
            max_concurrent_jobs=patch.max_concurrent_jobs,
            max_concurrent_translations=patch.max_concurrent_translations,
            free_max_concurrent_translations=patch.free_max_concurrent_translations,
            max_queued_jobs=patch.max_queued_jobs,
            free_max_queued_jobs=patch.free_max_queued_jobs,
            retention_hours=patch.retention_hours,
            min_free_disk_mb=patch.min_free_disk_mb,
            maintenance_mode=patch.maintenance_mode,
            site_notice=patch.site_notice.strip(),
        )
        with self._lock:
            old_system = self._snapshot.data.system
            changed_fields = [
                field for field in SystemConfig.model_fields if getattr(old_system, field) != getattr(new_system, field)
            ]
            new_data = self._snapshot.data.model_copy(update={"system": new_system})
            saved = self._commit_locked(
                expected_revision=patch.expected_revision,
                new_data=new_data,
                new_secrets=self._raw_secrets,
                section="system",
                changed_fields=changed_fields or ["system"],
                summary=(f"更新系统运行参数（维护模式：{'开启' if new_system.maintenance_mode else '关闭'}）"),
            )
        self._notify_limits(new_system, saved.revision)
        return self.admin_view()

    def reload_glossary(self) -> GlossaryAdminView:
        snap = self.snapshot()
        self._notify_glossary(list(snap.data.glossary.sources), snap.revision, force=True)
        return self._glossary_view(snap)

    def list_revisions(self, *, limit: int = 20, offset: int = 0) -> ConfigRevisionsOut:
        return ConfigRevisionsOut(
            items=self._store.list_revisions(limit=limit, offset=offset),
            limit=limit,
            offset=offset,
        )

    def rollback(self, req: RollbackIn) -> AdminConfigView:
        with self._lock:
            target_data = self._store.get_revision_data(req.target_revision)
            if target_data is None:
                raise UserError(f"找不到配置版本 r{req.target_revision}")

            # 校验目标版本的服务地址/协议是否与现存加密密钥或 .env 默认 MinerU Token 绑定冲突
            mineru_stored = self._raw_secrets.get(_MINERU_TOKEN_SLOT)
            target_mineru_url = target_data.parser.mineru_base_url.rstrip("/")
            if (
                mineru_stored is not None
                and mineru_stored.mode == "custom"
                and not mineru_stored.corrupted
                and mineru_stored.bound_url
                and mineru_stored.bound_url != target_mineru_url
            ):
                raise UserError("目标版本的 MinerU 服务地址与当前已存密钥绑定地址冲突，已拒绝回滚")
            if (
                (mineru_stored is None or (mineru_stored.mode == "inherit_env" and not mineru_stored.corrupted))
                and self._settings.mineru_api_token.strip()
                and target_mineru_url != _DEFAULT_MINERU_URL
            ):
                raise UserError(_ENV_MINERU_BIND_CONFLICT_MSG)

            for provider in target_data.free_translation.providers:
                stored = self._raw_secrets.get(_free_slot(provider.id))
                if (
                    stored is not None
                    and stored.mode == "custom"
                    and not stored.corrupted
                    and (
                        stored.bound_url != provider.base_url.rstrip("/") or stored.bound_protocol != provider.protocol
                    )
                ):
                    raise UserError(f"目标版本的免费渠道「{provider.id}」地址或协议与当前已存密钥绑定冲突，已拒绝回滚")

            candidate = self._build_snapshot(self._snapshot.revision + 1, target_data, self._raw_secrets, None)
            if target_data.free_translation.enabled and not candidate.active_free_providers():
                raise UserError("目标版本启用了免费翻译，但当前缺少有效渠道密钥，已拒绝回滚")

            old_data = self._snapshot.data
            saved = self._commit_locked(
                expected_revision=req.expected_revision,
                new_data=target_data,
                new_secrets=self._raw_secrets,
                section="rollback",
                changed_fields=["rollback"],
                summary=f"回滚配置至版本 r{req.target_revision}",
            )

        if old_data.system != target_data.system:
            self._notify_limits(target_data.system, saved.revision)
        if old_data.glossary.sources != target_data.glossary.sources:
            self._notify_glossary(target_data.glossary.sources, saved.revision)
        return self.admin_view()

    def build_free_secrets(
        self,
        preferred_provider_id: str | None = None,
        *,
        require_parser: bool = True,
        parser: str | None = None,
    ) -> Secrets:
        snap = self.snapshot()
        active = snap.active_free_providers()
        if not snap.data.free_translation.enabled or not active:
            raise UserError("免费体验通道当前不可用，请切换自备 API Key")
        if require_parser:
            if parser == "paddleocr-vl":
                if not snap.data.parser.paddle_public_enabled:
                    raise UserError("本站暂未向公众开放 PaddleOCR 解析服务")
                if not snap.paddle_token.value or snap.paddle_token.corrupted:
                    raise UserError("免费体验通道当前不可用（PaddleOCR 解析服务未就绪），请切换自备 API Key")
            elif parser == "mineru-cloud":
                if not snap.mineru_ready():
                    raise UserError("免费体验通道当前不可用（MinerU 解析服务未就绪），请切换其他解析服务或自备 API Key")
            elif not snap.public_parser_ready():
                raise UserError("免费体验通道当前不可用（解析服务未就绪），请切换自备 API Key")

        ordered = list(active)
        if preferred_provider_id:
            for idx, provider in enumerate(ordered):
                if provider.config.id == preferred_provider_id:
                    ordered.insert(0, ordered.pop(idx))
                    break

        primary = ordered[0]
        fallback_models = (primary.config.fallback_model,) if primary.config.fallback_model else ()
        fallback_endpoints: list[tuple[str, str, str, str, int]] = []
        for fb in ordered[1:]:
            fb_url = fb.config.base_url.rstrip("/")
            fallback_endpoints.append(
                (fb.config.protocol, fb_url, fb.api_key.value, fb.config.model, fb.config.timeout_s)
            )
            if fb.config.fallback_model:
                fallback_endpoints.append(
                    (fb.config.protocol, fb_url, fb.api_key.value, fb.config.fallback_model, fb.config.timeout_s)
                )

        return Secrets(
            llm_base_url=primary.config.base_url.rstrip("/"),
            llm_api_key=primary.api_key.value,
            llm_model=primary.config.model,
            parser_token="" if snap.mineru_token.corrupted else snap.mineru_token.value,
            paddle_token="" if snap.paddle_token.corrupted else snap.paddle_token.value,
            llm_protocol=primary.config.protocol,
            channel_mode="free",
            channel_provider_id=primary.config.id,
            fallback_models=fallback_models,
            fallback_endpoints=tuple(fallback_endpoints),
            llm_timeout_s=primary.config.timeout_s,
            mineru_base_url=snap.data.parser.mineru_base_url.rstrip("/"),
            mineru_proxy="" if snap.mineru_proxy.corrupted else snap.mineru_proxy.value,
            paddle_proxy="" if snap.paddle_proxy.corrupted else snap.paddle_proxy.value,
            disable_redirects=True,
            free_sdk_no_retry=True,
            allow_private_urls=self._settings.allow_private_llm_urls,
        )

    def _resolve_test_secret(
        self,
        *,
        label: str,
        update: SecretUpdate | None,
        current: ResolvedSecret | None,
        env_value: str,
        target_url: str,
        target_protocol: str,
        is_proxy: bool,
        allow_inherit_env: bool,
        env_bound_url: str = "",
    ) -> str:
        action = update.action if update is not None else "keep"
        norm_url = target_url.rstrip("/") if target_url else ""
        env_bound_conflict = bool(env_bound_url and env_value.strip() and norm_url and norm_url != env_bound_url)
        if action == "disable":
            return ""
        if action == "inherit_env":
            if not allow_inherit_env:
                raise UserError(f"{label}不支持继承环境变量")
            if env_bound_conflict:
                raise UserError(_ENV_MINERU_BIND_CONFLICT_MSG)
            return env_value.strip()
        if action == "replace":
            cleaned = (update.value if update is not None else "").strip()
            if not cleaned:
                raise UserError(f"请填写要测试的{label}")
            if is_proxy:
                try:
                    return validate_proxy_url(cleaned, allow_private=self._settings.allow_private_llm_urls)
                except ValueError as exc:
                    raise UserError(str(exc)) from exc
            return cleaned
        # action == "keep"
        if current is None:
            return ""
        if current.mode == "inherit_env" and env_bound_conflict:
            raise UserError(_ENV_MINERU_BIND_CONFLICT_MSG)
        if current.corrupted:
            raise UserError(f"{label}密文已损坏或缺少主密钥，无法沿用测试")
        if (
            current.mode == "custom"
            and norm_url
            and (current.bound_url != norm_url or (target_protocol and current.bound_protocol != target_protocol))
        ):
            raise UserError(_BIND_CONFLICT_MSG)
        return current.value

    async def test_connection(self, req: ConnectionTestIn) -> ConnectionTestOut:
        snap = self.snapshot()
        allow_private = self._settings.allow_private_llm_urls

        if req.target == "free_provider":
            existing = next((p for p in snap.free_providers if p.config.id == req.provider_id), None)
            protocol = req.protocol or (existing.config.protocol if existing else "openai")
            raw_url = req.base_url if req.base_url is not None else (existing.config.base_url if existing else "")
            model = (req.model if req.model is not None else (existing.config.model if existing else "")).strip()
            timeout_s = req.timeout_s or (existing.config.timeout_s if existing else 30)
            if not raw_url:
                raise UserError("请填写要测试的服务地址")
            if not model:
                raise UserError("请填写要测试的模型名")
            try:
                norm_url = validate_llm_url(raw_url, allow_private=allow_private, require_https=not allow_private)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
            api_key = self._resolve_test_secret(
                label="免费渠道 API Key",
                update=req.api_key,
                current=existing.api_key if existing else None,
                env_value="",
                target_url=norm_url,
                target_protocol=protocol,
                is_proxy=False,
                allow_inherit_env=False,
            )
            if not api_key:
                raise UserError("当前渠道未配置有效 API Key，无法测试连接")
            if self._chat_tester is not None:
                return await self._chat_tester(protocol, norm_url, api_key, model, min(timeout_s, 30))
            return await self._probe_chat(protocol, norm_url, api_key, model, timeout_s=min(timeout_s, 30))

        if req.target == "mineru":
            raw_url = req.base_url if req.base_url is not None else snap.data.parser.mineru_base_url
            try:
                norm_url = validate_parser_url(raw_url, allow_private=allow_private)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
            token = self._resolve_test_secret(
                label="MinerU Token",
                update=req.token,
                current=snap.mineru_token,
                env_value=self._settings.mineru_api_token,
                target_url=norm_url,
                target_protocol="mineru",
                is_proxy=False,
                allow_inherit_env=True,
                env_bound_url=_DEFAULT_MINERU_URL,
            )
            if not token:
                raise UserError("请先配置 MinerU Token 再测试连接")
            proxy = self._resolve_test_secret(
                label="MinerU 代理",
                update=req.proxy,
                current=snap.mineru_proxy,
                env_value=self._settings.mineru_proxy,
                target_url="",
                target_protocol="",
                is_proxy=True,
                allow_inherit_env=True,
            )
            return await self._probe_parser("mineru", norm_url, token, proxy)

        # target == "paddle"
        token = self._resolve_test_secret(
            label="PaddleOCR Token",
            update=req.token,
            current=snap.paddle_token,
            env_value=self._settings.paddle_token,
            target_url=PADDLE_JOB_URL,
            target_protocol="paddle",
            is_proxy=False,
            allow_inherit_env=True,
        )
        if not token:
            raise UserError("请先配置 PaddleOCR Token 再测试连接")
        proxy = self._resolve_test_secret(
            label="PaddleOCR 代理",
            update=req.proxy,
            current=snap.paddle_proxy,
            env_value=self._settings.paddle_proxy,
            target_url="",
            target_protocol="",
            is_proxy=True,
            allow_inherit_env=True,
        )
        return await self._probe_parser("paddle", PADDLE_JOB_URL, token, proxy)

    async def _probe_chat(
        self, protocol: str, base_url: str, api_key: str, model: str, *, timeout_s: int
    ) -> ConnectionTestOut:
        started = time.monotonic()
        chat: OpenAICompatChat | AnthropicChat | None = None
        allow_private = self._settings.allow_private_llm_urls
        try:
            if protocol == "anthropic":
                chat = AnthropicChat(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    timeout=timeout_s,
                    disable_redirects=True,
                    default_max_retries=0,
                    allow_private=allow_private,
                )
            else:
                chat = OpenAICompatChat(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    timeout=timeout_s,
                    disable_redirects=True,
                    default_max_retries=0,
                    allow_private=allow_private,
                )
            res = await chat.complete("只回复 OK 两个字母。", "ping", ChatOptions(max_tokens=16, transport_retries=0))
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            reply = res.text.strip()[:40]
            return ConnectionTestOut(
                ok=True,
                status_code=200,
                latency_ms=latency_ms,
                detail=f"连接正常（回复：{reply or 'OK'}）",
            )
        except ChatError as exc:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return ConnectionTestOut(
                ok=False,
                status_code=exc.status_code,
                latency_ms=latency_ms,
                detail=str(exc),
            )
        finally:
            if chat is not None:
                await chat.aclose()

    async def _probe_parser(
        self, target: Literal["mineru", "paddle"], base_url: str, token: str, proxy: str
    ) -> ConnectionTestOut:
        started = time.monotonic()
        allow_private = self._settings.allow_private_llm_urls
        try:
            if self._parser_tester is not None:
                status_code, latency_ms = await self._parser_tester(target, base_url, token, proxy)
            elif target == "mineru":
                status_code, latency_ms = await MinerUCloudParser(
                    token,
                    base_url=base_url,
                    proxy=proxy,
                    allow_private=allow_private,
                ).check_connection()
            else:
                status_code, latency_ms = await PaddleVLParser(
                    token,
                    proxy=proxy,
                    allow_private=allow_private,
                ).check_connection()
            name = "MinerU" if target == "mineru" else "PaddleOCR"
            return ConnectionTestOut(
                ok=True,
                status_code=status_code,
                latency_ms=latency_ms,
                detail=f"{name} 连接正常（HTTP {status_code}）",
            )
        except ParseError as exc:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return ConnectionTestOut(ok=False, status_code=None, latency_ms=latency_ms, detail=str(exc))

    async def list_models(self, req: AdminListModelsIn) -> AdminListModelsOut:
        """管理员专用拉取上游模型列表：支持免费渠道沿用已存密文或表单新 Key，以及自备预设公开/临时探测。"""
        snap = self.snapshot()
        allow_private = self._settings.allow_private_llm_urls
        try:
            norm_url = validate_llm_url(req.base_url, allow_private=allow_private, require_https=not allow_private)
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        if req.target == "free_provider":
            existing = next((p for p in snap.free_providers if p.config.id == req.provider_id), None)
            api_key = self._resolve_test_secret(
                label="免费渠道 API Key",
                update=req.api_key,
                current=existing.api_key if existing else None,
                env_value="",
                target_url=norm_url,
                target_protocol=req.protocol,
                is_proxy=False,
                allow_inherit_env=False,
            )
            if not api_key:
                raise UserError("当前渠道未配置有效 API Key，无法拉取模型列表")
            return await self._probe_models_with_key(req.protocol, norm_url, api_key)

        # target == "default_llm"：提供临时探测 Key 时直接用 Key 拉取，否则先尝试无鉴权公开 /models
        probe_key = req.probe_api_key.strip()
        if probe_key:
            return await self._probe_models_with_key(req.protocol, norm_url, probe_key)
        return await self._probe_public_models(req.protocol, norm_url)

    async def _probe_models_with_key(self, protocol: str, base_url: str, api_key: str) -> AdminListModelsOut:
        started = time.monotonic()
        chat: OpenAICompatChat | AnthropicChat | None = None
        allow_private = self._settings.allow_private_llm_urls
        try:
            if self._model_lister is not None:
                models = await self._model_lister(protocol, base_url, api_key)
            else:
                if protocol == "anthropic":
                    chat = AnthropicChat(
                        base_url=base_url,
                        api_key=api_key,
                        model="list-models",
                        timeout=MODEL_LIST_TIMEOUT_S,
                        disable_redirects=True,
                        default_max_retries=0,
                        allow_private=allow_private,
                    )
                else:
                    chat = OpenAICompatChat(
                        base_url=base_url,
                        api_key=api_key,
                        model="list-models",
                        timeout=MODEL_LIST_TIMEOUT_S,
                        disable_redirects=True,
                        default_max_retries=0,
                        allow_private=allow_private,
                    )
                models = await chat.list_models()
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return AdminListModelsOut(
                ok=True,
                models=models,
                latency_ms=latency_ms,
                detail=f"已拉取 {len(models)} 个可用模型" if models else "接口返回空模型列表，请手动填写",
            )
        except ChatError as exc:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return AdminListModelsOut(
                ok=False,
                models=[],
                latency_ms=latency_ms,
                detail=str(exc),
                requires_api_key=exc.status_code in (401, 403),
            )
        finally:
            if chat is not None:
                await chat.aclose()

    async def _probe_public_models(self, protocol: str, base_url: str) -> AdminListModelsOut:
        started = time.monotonic()
        allow_private = self._settings.allow_private_llm_urls
        if self._model_lister is not None:
            try:
                models = await self._model_lister(protocol, base_url, "")
                latency_ms = max(0, round((time.monotonic() - started) * 1000))
                return AdminListModelsOut(
                    ok=True,
                    models=models,
                    latency_ms=latency_ms,
                    detail=f"已拉取 {len(models)} 个公开模型" if models else "接口返回空模型列表，请手动填写",
                )
            except ChatError as exc:
                latency_ms = max(0, round((time.monotonic() - started) * 1000))
                return AdminListModelsOut(
                    ok=False,
                    models=[],
                    latency_ms=latency_ms,
                    detail=str(exc),
                    requires_api_key=exc.status_code in (401, 403),
                )

        trimmed = base_url.rstrip("/")
        if protocol == "anthropic" and not trimmed.endswith("/v1"):
            endpoint = f"{trimmed}/v1/models"
        else:
            endpoint = f"{trimmed}/models"
        headers: dict[str, str] = {"Accept": "application/json"}
        if protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        hooks = (
            {
                "request": [
                    make_outbound_request_hook(
                        allow_private=False,
                        require_https=not allow_private,
                        label="翻译服务地址",
                    )
                ]
            }
            if not allow_private
            else None
        )
        transport = build_safe_async_transport(allow_private=allow_private)
        try:
            async with httpx2.AsyncClient(
                follow_redirects=False,
                timeout=MODEL_LIST_TIMEOUT_S,
                transport=transport,
                event_hooks=hooks,
            ) as client:
                resp = await client.get(endpoint, headers=headers)
        except httpx2.TimeoutException:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return AdminListModelsOut(ok=False, latency_ms=latency_ms, detail="获取模型列表超时，请重试或手动填写")
        except httpx2.HTTPError:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return AdminListModelsOut(
                ok=False,
                latency_ms=latency_ms,
                detail="无法连接该服务地址，请检查 URL 或网络",
            )

        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        if resp.status_code in (401, 403):
            return AdminListModelsOut(
                ok=False,
                latency_ms=latency_ms,
                requires_api_key=True,
                detail="该服务商需要 API Key 才能查询模型列表，可输入一次性临时探测 Key（仅本次使用，绝不保存）",
            )
        if 300 <= resp.status_code < 400:
            return AdminListModelsOut(
                ok=False,
                latency_ms=latency_ms,
                detail=f"服务地址触发重定向（HTTP {resp.status_code}），出于安全策略已拒绝跟随",
            )
        if resp.status_code == 429:
            return AdminListModelsOut(
                ok=False,
                latency_ms=latency_ms,
                detail="模型列表请求受限（HTTP 429），请稍后重试或手动填写",
            )
        if resp.status_code != 200:
            return AdminListModelsOut(
                ok=False,
                latency_ms=latency_ms,
                detail=f"该服务暂不提供公开模型列表（HTTP {resp.status_code}），可提供临时探测 Key 或手动填写",
                requires_api_key=resp.status_code == 400,
            )
        try:
            body: Any = resp.json()
        except ValueError:
            return AdminListModelsOut(ok=False, latency_ms=latency_ms, detail="服务返回的模型列表格式无效，请手动填写")
        raw_items = body.get("data") if isinstance(body, dict) else body
        if not isinstance(raw_items, list):
            return AdminListModelsOut(ok=False, latency_ms=latency_ms, detail="服务返回的模型列表格式无效，请手动填写")
        ids = sorted(
            {
                item["id"].strip()
                for item in raw_items
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
            }
        )
        return AdminListModelsOut(
            ok=True,
            models=ids,
            latency_ms=latency_ms,
            detail=f"已拉取 {len(ids)} 个公开模型" if ids else "接口返回空模型列表，请手动填写",
        )
