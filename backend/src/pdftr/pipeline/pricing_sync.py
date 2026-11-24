"""远程 GitHub 模型单价源同步与本地缓存服务（支持 LiteLLM / models.dev / pdftr JSON）。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx2

from pdftr.domain.runtime_config import PricingSourceFormat, PricingSyncConfig, PricingSyncStatusView
from pdftr.domain.url_guard import build_safe_async_transport
from pdftr.pipeline.url_guard import make_outbound_request_hook, validate_outbound_request_url
from pdftr.translation.cost import Price

log = logging.getLogger(__name__)

MAX_PRICE_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB 防御体积上限
PRICE_FETCH_TIMEOUT_S = 20.0
_CHAT_MODES = frozenset({"", "chat", "completion", "responses"})


@dataclass(frozen=True)
class PriceFetchResult:
    status_code: int
    body: bytes = b""
    etag: str = ""
    not_modified: bool = False


PriceFetcher = Callable[[str, str], Awaitable[PriceFetchResult]]


@dataclass(frozen=True)
class RawPriceEntry:
    """远程解析后的单条单价记录：currency='usd' 时按百万 token 美元存底，改汇率即可实时重算 CNY。"""

    currency: Literal["usd", "cny"]
    cache_hit: float
    cache_miss: float
    output: float
    peak_factor: float = 1.0

    def to_cny_price(self, usd_to_cny: float) -> Price:
        factor = usd_to_cny if self.currency == "usd" else 1.0
        return Price(
            cache_hit=round(self.cache_hit * factor, 6),
            cache_miss=round(self.cache_miss * factor, 6),
            output=round(self.output * factor, 6),
            peak_factor=self.peak_factor,
        )


def validate_pricing_source_url(value: str, *, allow_private: bool = False) -> str:
    """校验远程价格源地址：留空表示停用远程同步；非空时强制公网 HTTPS（私有模式允许内网）。"""
    cleaned = value.strip()
    if not cleaned:
        return ""
    return validate_outbound_request_url(
        cleaned,
        allow_private=allow_private,
        require_https=not allow_private,
        label="模型单价源地址",
    )


def _finite_non_neg(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    if not math.isfinite(f) or f < 0:
        return None
    return f


def _parse_pdftr_entries(payload: Any) -> dict[str, RawPriceEntry]:
    raw_list: list[Any]
    if isinstance(payload, list):
        raw_list = payload
    elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
        raw_list = payload["models"]
    else:
        return {}
    result: dict[str, RawPriceEntry] = {}
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        model = item.get("model")
        if not isinstance(model, str) or not model.strip():
            continue
        cache_hit = _finite_non_neg(item.get("cache_hit"))
        cache_miss = _finite_non_neg(item.get("cache_miss"))
        output = _finite_non_neg(item.get("output"))
        peak = _finite_non_neg(item.get("peak_factor", 1.0))
        if cache_hit is None or cache_miss is None or output is None or peak is None or peak < 1.0 or peak > 10.0:
            continue
        result[model.strip()] = RawPriceEntry(
            currency="cny",
            cache_hit=cache_hit,
            cache_miss=cache_miss,
            output=output,
            peak_factor=peak,
        )
    return result


def _parse_litellm_entries(payload: Any) -> dict[str, RawPriceEntry]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, RawPriceEntry] = {}
    for model_id, item in payload.items():
        if not isinstance(model_id, str) or not model_id.strip() or model_id == "sample_spec":
            continue
        if not isinstance(item, dict):
            continue
        raw_mode = item.get("mode")
        mode = raw_mode.strip().casefold() if isinstance(raw_mode, str) else ""
        if mode not in _CHAT_MODES:
            continue
        in_tok = _finite_non_neg(item.get("input_cost_per_token"))
        out_tok = _finite_non_neg(item.get("output_cost_per_token"))
        if in_tok is None or out_tok is None:
            continue
        cache_tok = _finite_non_neg(item.get("cache_read_input_token_cost"))
        if cache_tok is None:
            cache_tok = _finite_non_neg(item.get("input_cost_per_token_cache_hit"))
        if cache_tok is None:
            cache_tok = in_tok
        result[model_id.strip()] = RawPriceEntry(
            currency="usd",
            cache_hit=cache_tok * 1_000_000,
            cache_miss=in_tok * 1_000_000,
            output=out_tok * 1_000_000,
            peak_factor=1.0,
        )
    return result


def _parse_models_dev_entries(payload: Any) -> dict[str, RawPriceEntry]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, RawPriceEntry] = {}

    def _consume_model(model_id: str, meta: Any, prefix: str = "") -> None:
        if not isinstance(meta, dict):
            return
        cost = meta.get("cost")
        if not isinstance(cost, dict):
            return
        in_m = _finite_non_neg(cost.get("input"))
        out_m = _finite_non_neg(cost.get("output"))
        if in_m is None or out_m is None:
            return
        cache_m = _finite_non_neg(cost.get("cache_read"))
        if cache_m is None:
            cache_m = in_m
        entry = RawPriceEntry(currency="usd", cache_hit=cache_m, cache_miss=in_m, output=out_m, peak_factor=1.0)
        key = model_id.strip()
        if not key:
            return
        result[key] = entry
        if prefix and "/" not in key:
            result.setdefault(f"{prefix}/{key}", entry)

    for top_key, top_val in payload.items():
        if not isinstance(top_key, str) or not isinstance(top_val, dict):
            continue
        models_map = top_val.get("models")
        if isinstance(models_map, dict):
            for mid, mval in models_map.items():
                if isinstance(mid, str):
                    _consume_model(mid, mval, prefix=top_key.strip())
        elif "cost" in top_val:
            _consume_model(top_key, top_val)
    return result


def parse_remote_price_payload(
    raw_bytes: bytes,
    *,
    source_format: PricingSourceFormat = "auto",
) -> tuple[dict[str, RawPriceEntry], Literal["litellm", "models_dev", "pdftr"]]:
    """解析远程模型单价 JSON，返回 (原始单价映射, 实际识别格式)。解析失败抛出 ValueError。"""
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("远程价格源返回的不是有效的 UTF-8 JSON 数据") from exc

    if source_format == "pdftr":
        parsed = _parse_pdftr_entries(payload)
        if not parsed:
            raise ValueError("未在价格源中找到有效的 pdftr 格式模型单价条目")
        return parsed, "pdftr"
    if source_format == "litellm":
        parsed = _parse_litellm_entries(payload)
        if not parsed:
            raise ValueError("未在价格源中找到有效的 LiteLLM 模型单价条目")
        return parsed, "litellm"
    if source_format == "models_dev":
        parsed = _parse_models_dev_entries(payload)
        if not parsed:
            raise ValueError("未在价格源中找到有效的 models.dev 模型单价条目")
        return parsed, "models_dev"

    # auto：按结构特征依次识别
    pdftr_entries = _parse_pdftr_entries(payload)
    if pdftr_entries:
        return pdftr_entries, "pdftr"
    litellm_entries = _parse_litellm_entries(payload)
    if litellm_entries:
        return litellm_entries, "litellm"
    models_dev_entries = _parse_models_dev_entries(payload)
    if models_dev_entries:
        return models_dev_entries, "models_dev"
    raise ValueError("无法识别远程价格源的 JSON 格式，或源中不包含有效的对话模型单价")


async def _default_fetch_remote_prices(
    url: str,
    etag: str,
    *,
    allow_private: bool,
    timeout_s: float = PRICE_FETCH_TIMEOUT_S,
    max_bytes: int = MAX_PRICE_RESPONSE_BYTES,
) -> PriceFetchResult:
    headers = {"Accept": "application/json", "User-Agent": "pdf-translator/pricing-sync"}
    if etag:
        headers["If-None-Match"] = etag
    hooks = (
        {
            "request": [
                make_outbound_request_hook(
                    allow_private=False,
                    require_https=True,
                    label="模型单价源地址",
                )
            ]
        }
        if not allow_private
        else None
    )
    transport = build_safe_async_transport(allow_private=allow_private)
    async with (
        httpx2.AsyncClient(
            follow_redirects=False,
            timeout=timeout_s,
            transport=transport,
            event_hooks=hooks,
        ) as client,
        client.stream("GET", url, headers=headers) as resp,
    ):
        if resp.status_code == 304:
            return PriceFetchResult(status_code=304, etag=resp.headers.get("etag", etag), not_modified=True)
        if 300 <= resp.status_code < 400:
            raise ValueError(f"远程价格源触发重定向（HTTP {resp.status_code}），出于安全策略已拒绝跟随")
        if resp.status_code != 200:
            raise ValueError(f"远程价格源请求失败（HTTP {resp.status_code}）")
        content_length = resp.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > max_bytes:
            raise ValueError("远程价格源响应体超过 10 MB 安全限制")
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ValueError("远程价格源响应体超过 10 MB 安全限制")
        return PriceFetchResult(
            status_code=200,
            body=bytes(buf),
            etag=resp.headers.get("etag", ""),
            not_modified=False,
        )


class PricingSyncService:
    """管理远程模型单价同步状态与本地文件缓存（{data_dir}/pricing/remote_prices.json）。"""

    def __init__(
        self,
        cache_path: Path,
        *,
        allow_private: bool = False,
        fetcher: PriceFetcher | None = None,
    ) -> None:
        self._cache_path = cache_path
        self._allow_private = allow_private
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._sync_lock: asyncio.Lock | None = None
        self._syncing = False
        self._source_url = ""
        self._detected_format: Literal["litellm", "models_dev", "pdftr"] = "litellm"
        self._etag = ""
        self._synced_at: datetime | None = None
        self._last_error = ""
        self._entries: dict[str, RawPriceEntry] = {}
        self._load_cache_from_disk()

    def _load_cache_from_disk(self) -> None:
        if not self._cache_path.is_file():
            return
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("读取远程模型单价缓存失败，已忽略损坏缓存：%s", self._cache_path)
            return
        if not isinstance(raw, dict):
            return
        entries: dict[str, RawPriceEntry] = {}
        raw_entries = raw.get("entries")
        if isinstance(raw_entries, dict):
            for model, item in raw_entries.items():
                if not isinstance(model, str) or not isinstance(item, dict):
                    continue
                currency = item.get("currency")
                if currency not in ("usd", "cny"):
                    continue
                cache_hit = _finite_non_neg(item.get("cache_hit"))
                cache_miss = _finite_non_neg(item.get("cache_miss"))
                output = _finite_non_neg(item.get("output"))
                peak = _finite_non_neg(item.get("peak_factor", 1.0))
                if cache_hit is None or cache_miss is None or output is None or peak is None or peak < 1.0:
                    continue
                entries[model] = RawPriceEntry(
                    currency=currency,
                    cache_hit=cache_hit,
                    cache_miss=cache_miss,
                    output=output,
                    peak_factor=peak,
                )
        synced_raw = raw.get("synced_at")
        synced_at: datetime | None = None
        if isinstance(synced_raw, str) and synced_raw:
            try:
                synced_at = datetime.fromisoformat(synced_raw)
            except ValueError:
                synced_at = None
        fmt = raw.get("detected_format")
        detected_format: Literal["litellm", "models_dev", "pdftr"] = (
            fmt if fmt in ("litellm", "models_dev", "pdftr") else "litellm"
        )
        with self._lock:
            self._source_url = str(raw.get("source_url", ""))
            self._detected_format = detected_format
            self._etag = str(raw.get("etag", ""))
            self._synced_at = synced_at
            self._last_error = str(raw.get("last_error", ""))
            self._entries = entries

    def _save_cache_to_disk(self) -> None:
        with self._lock:
            payload = {
                "source_url": self._source_url,
                "detected_format": self._detected_format,
                "etag": self._etag,
                "synced_at": self._synced_at.isoformat() if self._synced_at is not None else None,
                "last_error": self._last_error,
                "entries": {
                    model: {
                        "currency": entry.currency,
                        "cache_hit": entry.cache_hit,
                        "cache_miss": entry.cache_miss,
                        "output": entry.output,
                        "peak_factor": entry.peak_factor,
                    }
                    for model, entry in self._entries.items()
                },
            }
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(tmp, self._cache_path)

    def prices_cny(self, usd_to_cny: float) -> dict[str, Price]:
        with self._lock:
            if not self._entries:
                return {}
            return {model: entry.to_cny_price(usd_to_cny) for model, entry in self._entries.items()}

    def status_view(self, configured_url: str) -> PricingSyncStatusView:
        with self._lock:
            effective_url = configured_url.strip() or self._source_url
            if self._syncing:
                status: Literal["never", "ok", "error", "syncing"] = "syncing"
            elif self._last_error:
                status = "error"
            elif self._synced_at is not None and self._entries:
                status = "ok"
            else:
                status = "never"
            return PricingSyncStatusView(
                source_url=effective_url,
                status=status,
                synced_at=self._synced_at,
                model_count=len(self._entries),
                last_error=self._last_error,
                syncing=self._syncing,
            )

    def is_due(self, cfg: PricingSyncConfig, *, now: datetime | None = None) -> bool:
        url = cfg.source_url.strip()
        if not cfg.auto_sync_enabled or not url:
            return False
        current = now or datetime.now(UTC)
        with self._lock:
            if self._syncing:
                return False
            if self._synced_at is None or self._source_url != url:
                return True
            synced = self._synced_at if self._synced_at.tzinfo is not None else self._synced_at.replace(tzinfo=UTC)
            return current - synced >= timedelta(hours=cfg.sync_interval_hours)

    async def sync_now(self, cfg: PricingSyncConfig, *, force: bool = False) -> PricingSyncStatusView:
        try:
            url = validate_pricing_source_url(cfg.source_url, allow_private=self._allow_private)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if not url:
            raise ValueError("请先配置有效的远程模型单价源地址")

        if self._sync_lock is None:
            self._sync_lock = asyncio.Lock()

        async with self._sync_lock:
            with self._lock:
                etag_to_send = "" if (force or self._source_url != url) else self._etag
                self._syncing = True
            try:
                if self._fetcher is not None:
                    result = await self._fetcher(url, etag_to_send)
                else:
                    result = await _default_fetch_remote_prices(
                        url,
                        etag_to_send,
                        allow_private=self._allow_private,
                    )
                now = datetime.now(UTC)
                if result.not_modified:
                    with self._lock:
                        self._source_url = url
                        self._etag = result.etag or self._etag
                        self._synced_at = now
                        self._last_error = ""
                        self._syncing = False
                    await asyncio.to_thread(self._save_cache_to_disk)
                    return self.status_view(url)

                entries, detected_format = parse_remote_price_payload(result.body, source_format=cfg.source_format)
                with self._lock:
                    self._source_url = url
                    self._detected_format = detected_format
                    self._etag = result.etag
                    self._synced_at = now
                    self._last_error = ""
                    self._entries = entries
                    self._syncing = False
                await asyncio.to_thread(self._save_cache_to_disk)
                log.info("远程模型单价同步成功：来源=%s 格式=%s 模型数=%d", url, detected_format, len(entries))
                return self.status_view(url)
            except Exception as exc:
                err_msg = str(exc).strip() or "远程模型单价同步失败"
                if isinstance(exc, httpx2.TimeoutException):
                    err_msg = "拉取远程模型单价超时，请稍后重试"
                elif isinstance(exc, httpx2.HTTPError):
                    err_msg = "无法连接远程价格源，请检查网络或源地址"
                with self._lock:
                    self._last_error = err_msg[:300]
                    self._syncing = False
                try:
                    await asyncio.to_thread(self._save_cache_to_disk)
                except OSError:
                    log.warning("保存同步错误状态失败", exc_info=True)
                log.warning("远程模型单价同步失败（已保留现有缓存）：%s", err_msg)
                return self.status_view(url)
