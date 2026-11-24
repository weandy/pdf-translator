"""组合根：在这里创建适配器并注入，其他模块只依赖端口。"""

import logging
import threading
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from pdftr.config import Settings, decode_config_secret_key
from pdftr.domain.llm import ChatModel
from pdftr.glossary import sources as glossary_sources
from pdftr.glossary.matcher import TermMatcher
from pdftr.parsing.mineru_cloud import MinerUCloudParser, MinerUResourceLimits
from pdftr.parsing.paddle_vl import PaddleVLParser
from pdftr.parsing.port import LayoutParser
from pdftr.pipeline.admin import AdminAuth
from pdftr.pipeline.events import EventBus
from pdftr.pipeline.runner import Pipeline, PipelineDeps
from pdftr.pipeline.runtime_config import RuntimeConfigManager
from pdftr.pipeline.secrets import Secrets, SecretStore
from pdftr.pipeline.usecases import UserError
from pdftr.storage.admin import SqliteAdminSessionStore
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.files import FileArtifactStore
from pdftr.storage.layout_cache import SqliteLayoutCache
from pdftr.storage.runtime_config import (
    CONFIG_SECRET_KEY_FILENAME,
    SqliteRuntimeConfigStore,
    load_or_create_local_secret_key,
)
from pdftr.storage.sqlite import SqliteStore
from pdftr.translation.anthropic_compat import AnthropicChat
from pdftr.translation.openai_compat import FallbackChat, OpenAICompatChat

log = logging.getLogger(__name__)


class GlossaryLoader:
    """下载并合并各术语库来源；支持运行时异步热重载与代次去重，旧代次结果绝不回写覆盖新配置。"""

    RETRY_AFTER_S = 1800

    def __init__(self, settings: Settings) -> None:
        self._vendor = settings.data_dir / "glossaries" / "vendor"
        self._sources = glossary_sources.parse_ids(settings.glossary_sources)
        self._lock = threading.Lock()
        self._load_done = threading.Condition(self._lock)
        self._generation = 0
        self._last_glossary_revision = -1
        self._loading_generation: int | None = None
        self._matcher: TermMatcher | None = None
        self._term_count = 0
        self._loaded_sources: list[str] = []
        self._reloading = False
        self._worker_running = False
        self._reload_worker: threading.Thread | None = None
        self._pending_reload = False
        self._retry_at: float | None = 0.0  # None = 全部加载成功，不再重试

    def set_initial_sources(self, source_ids: Sequence[str], *, revision: int = 0) -> None:
        with self._lock:
            self._generation += 1
            self._last_glossary_revision = max(self._last_glossary_revision, revision)
            self._sources = [glossary_sources.SOURCES[sid] for sid in source_ids if sid in glossary_sources.SOURCES]
            if not self._sources:
                self._matcher = None
                self._term_count = 0
                self._loaded_sources = []
                self._retry_at = None

    def has_cache(self, source_id: str) -> bool:
        src = glossary_sources.SOURCES.get(source_id)
        return bool(src is not None and (self._vendor / src.cache_name).exists())

    def status(self) -> tuple[int, bool, list[str]]:
        with self._lock:
            return self._term_count, self._reloading, list(self._loaded_sources)

    def _load_sources(self, targets: list[glossary_sources.Source]) -> tuple[TermMatcher | None, int, list[str], bool]:
        if not targets:
            return None, 0, [], True
        terms, loaded = glossary_sources.load_all(targets, self._vendor)
        matcher = TermMatcher(terms) if terms else None
        if terms:
            log.info("术语库：%s，共 %d 条", "、".join(s.id for s in loaded), len(terms))
        return matcher, len(terms), [s.id for s in loaded], len(loaded) == len(targets)

    def reload_sources(
        self,
        source_ids: Sequence[str],
        revision: int | None = None,
        *,
        force: bool = False,
    ) -> bool:
        """更新来源列表并在后台守护线程异步下载合并；拒绝落后于已应用版本的迟到通知。"""
        targets = [glossary_sources.SOURCES[sid] for sid in source_ids if sid in glossary_sources.SOURCES]
        with self._lock:
            if revision is not None:
                if revision < self._last_glossary_revision:
                    return False
                if revision == self._last_glossary_revision and not force:
                    return False
                self._last_glossary_revision = revision
            self._generation += 1
            self._sources = targets
            if not targets:
                self._matcher = None
                self._term_count = 0
                self._loaded_sources = []
                self._retry_at = None
                self._pending_reload = False
                self._reloading = False
                self._load_done.notify_all()
                return True
            self._reloading = True
            if self._worker_running:
                self._pending_reload = True
                return True
            self._worker_running = True
            self._pending_reload = False
            worker = threading.Thread(target=self._run_reload_loop, name="glossary-reload", daemon=True)
            self._reload_worker = worker
            worker.start()
            return True

    def _run_reload_loop(self) -> None:
        while True:
            with self._lock:
                gen = self._generation
                targets = list(self._sources)
                self._pending_reload = False
                if not targets:
                    self._reloading = False
                    self._worker_running = False
                    if self._loading_generation == gen:
                        self._loading_generation = None
                    self._load_done.notify_all()
                    return
                self._loading_generation = gen
            try:
                matcher, count, loaded_ids, complete = self._load_sources(targets)
            except Exception:
                log.exception("术语库后台重载失败")
                matcher, count, loaded_ids, complete = None, 0, [], False
                failed = True
            else:
                failed = False
            with self._lock:
                if self._loading_generation == gen:
                    self._loading_generation = None
                if gen == self._generation:
                    if not failed:
                        self._matcher = matcher
                        self._term_count = count
                        self._loaded_sources = loaded_ids
                        self._retry_at = None if complete else time.monotonic() + self.RETRY_AFTER_S
                    else:
                        self._retry_at = time.monotonic() + self.RETRY_AFTER_S
                    self._load_done.notify_all()
                    if not self._pending_reload:
                        self._reloading = False
                        self._worker_running = False
                        return
                else:
                    # 后台加载完成时代次已过期，丢弃旧结果，避免覆盖新配置（含已清空来源）。
                    self._load_done.notify_all()
                    if not self._pending_reload:
                        if not self._sources:
                            self._reloading = False
                        self._worker_running = False
                        return

    def __call__(self) -> TermMatcher | None:
        with self._lock:
            while True:
                if not self._sources:
                    return None
                if self._loading_generation == self._generation:
                    self._load_done.wait()
                    continue
                if self._retry_at is None or time.monotonic() < self._retry_at:
                    return self._matcher
                gen = self._generation
                targets = list(self._sources)
                self._loading_generation = gen
                break
        try:
            matcher, count, loaded_ids, complete = self._load_sources(targets)
        except Exception:
            with self._lock:
                if self._loading_generation == gen:
                    self._loading_generation = None
                self._load_done.notify_all()
            raise
        with self._lock:
            if self._loading_generation == gen:
                self._loading_generation = None
            if gen == self._generation:
                if matcher is not None or not targets:
                    self._matcher = matcher
                    self._term_count = count
                    self._loaded_sources = loaded_ids
                self._retry_at = None if complete else time.monotonic() + self.RETRY_AFTER_S
            self._load_done.notify_all()
            return self._matcher


def _build_single_chat(
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    *,
    timeout_s: int | None,
    disable_redirects: bool,
    free_sdk_no_retry: bool,
    allow_private: bool = False,
) -> ChatModel:
    timeout = float(timeout_s or 600)
    call_timeout = float(timeout_s) if timeout_s is not None else None
    retries = 0 if free_sdk_no_retry else 3
    if protocol == "anthropic":
        return AnthropicChat(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            call_timeout_s=call_timeout,
            disable_redirects=disable_redirects,
            default_max_retries=retries,
            allow_private=allow_private,
        )
    return OpenAICompatChat(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        call_timeout_s=call_timeout,
        disable_redirects=disable_redirects,
        default_max_retries=retries,
        allow_private=allow_private,
    )


def _default_chat_factory(sec: Secrets) -> ChatModel:
    primary = _build_single_chat(
        sec.llm_protocol,
        sec.llm_base_url,
        sec.llm_api_key,
        sec.llm_model,
        timeout_s=sec.llm_timeout_s,
        disable_redirects=sec.disable_redirects,
        free_sdk_no_retry=sec.free_sdk_no_retry,
        allow_private=sec.allow_private_urls,
    )
    if sec.channel_mode != "free" or (not sec.fallback_models and not sec.fallback_endpoints):
        return primary
    fallbacks: list[ChatModel] = []
    for fb_model in sec.fallback_models:
        if not fb_model or fb_model == sec.llm_model:
            continue
        fallbacks.append(
            _build_single_chat(
                sec.llm_protocol,
                sec.llm_base_url,
                sec.llm_api_key,
                fb_model,
                timeout_s=sec.llm_timeout_s,
                disable_redirects=sec.disable_redirects,
                free_sdk_no_retry=sec.free_sdk_no_retry,
                allow_private=sec.allow_private_urls,
            )
        )
    for protocol, base_url, api_key, model, timeout_s in sec.fallback_endpoints:
        fallbacks.append(
            _build_single_chat(
                protocol,
                base_url,
                api_key,
                model,
                timeout_s=timeout_s,
                disable_redirects=sec.disable_redirects,
                free_sdk_no_retry=sec.free_sdk_no_retry,
                allow_private=sec.allow_private_urls,
            )
        )
    if not fallbacks:
        return primary
    primary_timeout = float(sec.llm_timeout_s) if sec.llm_timeout_s else None
    return FallbackChat(primary, fallbacks, primary_timeout_s=primary_timeout)


def _default_parser_factory(sec: Secrets, name: str) -> LayoutParser:
    if name == "paddleocr-vl":
        return PaddleVLParser(sec.paddle_token, proxy=sec.paddle_proxy, allow_private=sec.allow_private_urls)
    return MinerUCloudParser(
        sec.parser_token,
        base_url=sec.mineru_base_url,
        proxy=sec.mineru_proxy,
        allow_private=sec.allow_private_urls,
    )


@dataclass(frozen=True)
class Container:
    settings: Settings
    pipeline: Pipeline
    admin: AdminAuth | None = None
    runtime_config: RuntimeConfigManager | None = None

    def secrets(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        model: str | None,
        parser_token: str | None,
        paddle_token: str | None = None,
        protocol: str | None = None,
    ) -> Secrets:
        """自备 Key 路径凭据：浏览器请求头优先，其次热配置默认值与站点解析配置。

        .env 共享的翻译 Key 严格只在请求目标与 .env 的 llm_base_url + llm_protocol 一致时下发。
        """
        s = self.settings
        snap = self.runtime_config.snapshot() if self.runtime_config is not None else None
        default_url = (snap.data.default_llm.base_url if snap is not None else s.llm_base_url).rstrip("/")
        default_protocol = snap.data.default_llm.protocol if snap is not None else s.llm_protocol
        default_model = snap.data.default_llm.model if snap is not None else s.llm_model

        url = (base_url or default_url).rstrip("/")
        proto = protocol or default_protocol
        same_env_service = url == s.llm_base_url.rstrip("/") and proto == s.llm_protocol

        site_mineru = snap.mineru_token.value if snap is not None else s.mineru_api_token
        site_paddle = snap.paddle_token.value if snap is not None else s.paddle_token
        mineru_base_url = snap.data.parser.mineru_base_url.rstrip("/") if snap is not None else "https://mineru.net"
        mineru_proxy = snap.mineru_proxy.value if snap is not None else s.mineru_proxy
        paddle_proxy = snap.paddle_proxy.value if snap is not None else s.paddle_proxy

        return Secrets(
            llm_base_url=url,
            llm_api_key=api_key or (s.llm_api_key if same_env_service else ""),
            llm_model=model or default_model,
            parser_token=parser_token or site_mineru,
            paddle_token=paddle_token or site_paddle,
            llm_protocol=proto,
            channel_mode="byok",
            mineru_base_url=mineru_base_url,
            mineru_proxy=mineru_proxy,
            paddle_proxy=paddle_proxy,
            disable_redirects=True,
            allow_private_urls=s.allow_private_llm_urls,
        )

    def free_secrets(
        self,
        preferred_provider_id: str | None = None,
        *,
        require_parser: bool = True,
        parser: str | None = None,
    ) -> Secrets:
        """站点免费体验通道凭据：始终来自热配置快照，彻底忽略浏览器请求头。"""
        if self.runtime_config is None:
            raise UserError("免费体验通道当前不可用，请切换自备 API Key")
        return self.runtime_config.build_free_secrets(
            preferred_provider_id,
            require_parser=require_parser,
            parser=parser,
        )

    def close(self) -> None:
        """统一释放所有底层 SQLite 存储连接（Windows 下便于清理临时目录）。"""
        for resource in (self.pipeline.deps.store, self.pipeline.deps.cache, self.pipeline.deps.layout_cache):
            closer = getattr(resource, "close", None)
            if callable(closer):
                closer()
        if self.admin is not None:
            self.admin.store.close()
        if self.runtime_config is not None:
            self.runtime_config.close()


def build_container(settings: Settings | None = None, deps_override: dict | None = None) -> Container:
    s = settings or Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    db = s.data_dir / "app.db"
    pipeline_ref: list[Pipeline] = []

    @asynccontextmanager
    async def local_slot() -> AsyncIterator[None]:
        async with pipeline_ref[0].local_slot():
            yield

    mineru_limits = MinerUResourceLimits(local_slot=local_slot)
    glossary_loader = GlossaryLoader(s)
    runtime_store = SqliteRuntimeConfigStore(db)
    secret_key = decode_config_secret_key(s.config_secret_key)
    secret_key_source: Literal["env", "file", "unavailable"]
    secret_key_warning = ""
    if secret_key is not None:
        secret_key_source = "env"
    else:
        secret_key, secret_key_warning = load_or_create_local_secret_key(s.data_dir / CONFIG_SECRET_KEY_FILENAME)
        secret_key_source = "file" if secret_key is not None else "unavailable"
    runtime_config = RuntimeConfigManager(
        settings=s,
        store=runtime_store,
        secret_key=secret_key,
        secret_key_source=secret_key_source,
        secret_key_warning=secret_key_warning,
        glossary_status=lambda: glossary_loader.status()[:2],
        glossary_cached=glossary_loader.has_cache,
    )
    snap = runtime_config.snapshot()
    if snap.revision > 0:
        glossary_loader.set_initial_sources(snap.data.glossary.sources, revision=snap.revision)

    parse_limit = snap.data.system.max_concurrent_jobs if snap.revision > 0 else s.max_concurrent_jobs
    byok_limit = snap.data.system.max_concurrent_translations if snap.revision > 0 else s.max_concurrent_translations
    free_limit = snap.data.system.free_max_concurrent_translations
    retention_hours = snap.data.system.retention_hours if snap.revision > 0 else s.retention_hours
    min_free_disk_mb = snap.data.system.min_free_disk_mb if snap.revision > 0 else s.min_free_disk_mb

    cache_path = s.data_dir / "layout_cache.db"
    cache_ttl = min(retention_hours, s.layout_cache_ttl_hours)
    layout_cache = (
        SqliteLayoutCache(cache_path, min_free_disk_mb=min_free_disk_mb) if cache_ttl or cache_path.exists() else None
    )
    if layout_cache is not None and cache_ttl == 0:
        layout_cache.clear_all()

    def _parser_factory(sec: Secrets, name: str) -> LayoutParser:
        cur = runtime_config.snapshot()
        if name == "paddleocr-vl":
            return PaddleVLParser(
                sec.paddle_token,
                proxy=sec.paddle_proxy or cur.paddle_proxy.value,
                allow_private=s.allow_private_llm_urls,
            )
        return MinerUCloudParser(
            sec.parser_token,
            base_url=sec.mineru_base_url or cur.data.parser.mineru_base_url,
            proxy=sec.mineru_proxy or cur.mineru_proxy.value,
            allow_private=s.allow_private_llm_urls,
            use_pdf_parent_tmp=True,
            enable_partial_pages=s.mineru_partial_pages_enabled,
            enable_sharding=s.mineru_sharding_enabled,
            shard_threshold=s.mineru_shard_threshold,
            shard_target_pages=s.mineru_shard_target_pages,
            shard_min_pages=s.mineru_shard_min_pages,
            max_shards=s.mineru_max_shards,
            limits=mineru_limits,
        )

    deps = {
        "artifacts": FileArtifactStore(s.data_dir),
        "store": SqliteStore(db, submission_audit_enabled=s.submission_audit_enabled),
        "cache": SqliteTranslationCache(db),
        "layout_cache": layout_cache,
        "layout_cache_ttl_hours": cache_ttl,
        "events": EventBus(),
        "secrets": SecretStore(),
        "parser_factory": _parser_factory,
        "chat_factory": _default_chat_factory,
        "term_matcher": glossary_loader,
        "max_concurrent_jobs": parse_limit,
        "max_concurrent_translations": byok_limit,
        "free_max_concurrent_translations": free_limit,
        "font_dir": s.font_dir,
        "llm_concurrency": s.llm_concurrency,
        "mineru_partial_pages_enabled": s.mineru_partial_pages_enabled,
        "price_overrides_loader": lambda: runtime_config.snapshot().price_overrides,
    } | (deps_override or {})  # 测试时替换解析器、大模型等
    pipeline = Pipeline(PipelineDeps(**deps))
    pipeline_ref.append(pipeline)

    def _on_limits_changed(sys_cfg, rev: int) -> None:
        pipeline.apply_runtime_limits(
            sys_cfg.max_concurrent_jobs,
            sys_cfg.max_concurrent_translations,
            sys_cfg.free_max_concurrent_translations,
            revision=rev,
        )

    def _on_glossary_changed(sources: tuple[str, ...], rev: int) -> None:
        glossary_loader.reload_sources(sources, rev)

    def _on_glossary_reload(sources: tuple[str, ...], rev: int) -> None:
        glossary_loader.reload_sources(sources, rev, force=True)

    runtime_config.set_callbacks(
        on_limits_changed=_on_limits_changed,
        on_glossary_changed=_on_glossary_changed,
        on_glossary_reload=_on_glossary_reload,
    )
    admin = (
        AdminAuth(SqliteAdminSessionStore(db), s.admin_password_hash, s.admin_origin, s.admin_session_days * 86400)
        if s.admin_password_hash and s.admin_origin
        else None
    )
    return Container(settings=s, pipeline=pipeline, admin=admin, runtime_config=runtime_config)
