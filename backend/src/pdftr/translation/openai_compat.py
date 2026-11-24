"""OpenAI 兼容接口适配器。

各家“思考”开关的写法不同（参考 zotero-pdf-translate、KISS Translator 的做法）：
- deepseek：extra_body.thinking = {"type": "enabled"|"disabled"}，开启时 reasoning_effort=low
- openai：reasoning_effort = "none"|"low"，推理模型不接受 temperature
- nemotron：关思考时 extra_body.chat_template_kwargs = {"enable_thinking": False} + temperature，开启时 reasoning_effort
- nemotron_off：全阶段强制关闭思考，extra_body.chat_template_kwargs = {"enable_thinking": False} + temperature
- generic：不发送任何思考参数，交给接口默认（未知服务发了不认识的参数可能报错）
"""

import asyncio
import logging
import re
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

import httpx2
import openai
from openai import AsyncOpenAI

from pdftr.domain.llm import ChatError, ChatModel, ChatOptions, ChatResult, ChatUsage
from pdftr.domain.url_guard import build_safe_async_transport, validate_outbound_request_url

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

Dialect = Literal["deepseek", "openai", "nemotron", "nemotron_off", "generic"]
MODEL_LIST_TIMEOUT_S = 15
log = logging.getLogger(__name__)

_SEMANTIC_FALLBACKS = frozenset({"drop_thinking", "drop_reasoning_effort", "drop_output_config", "drop_temperature"})
_FALLBACK_BY_FIELD = {
    "max_tokens": "use_max_completion_tokens",
    "reasoning_effort": "drop_reasoning_effort",
    "thinking": "drop_thinking",
    "temperature": "drop_temperature",
}
_NON_RETRYABLE_429_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached", "account_deactivated"})
_CONTEXT_LIMIT = re.compile(r"context_length_exceeded|maximum context length|too many tokens|prompt is too long")
_INCOMPATIBLE = re.compile(
    r"\b(?:unsupported|unknown|unrecognized|not\s+(?:supported|allowed)|extra|unexpected|invalid)\b"
)


def _mentions(message: str, field: str) -> bool:
    return re.search(rf"(?<![\w-]){field}(?![\w-])", message) is not None


def _incompatible_fields(body: object, fields: set[str], *, aliases: dict[str, str] | None = None) -> set[str]:
    # SDK 可能已经解开外层 error，兼容接口也可能直接返回错误字段。
    error = body.get("error", body) if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return set()
    if any(
        isinstance(value := error.get(key), str) and _CONTEXT_LIMIT.search(value.casefold())
        for key in ("code", "type", "message")
    ):
        return set()
    raw_message = error.get("message")
    message = raw_message.casefold() if isinstance(raw_message, str) else ""
    incompatible = _INCOMPATIBLE.search(message) is not None
    return {
        field
        for field in fields
        if (
            error.get("param") == field
            or (
                _mentions(message, field)
                and (incompatible or (field == "max_tokens" and _mentions(message, "max_completion_tokens")))
            )
            or (incompatible and aliases is not None and field in aliases and _mentions(message, aliases[field]))
        )
    }


def _sent_fields(kw: dict[str, Any]) -> set[str]:
    return kw.keys() | (kw.get("extra_body") or {}).keys()


def _semantic_fallback(original: set[str], sent: set[str], fallback_by_field: dict[str, str]) -> bool:
    # 只比较成功请求的快照，避免并发返回期间更新的实例记忆误标本次结果。
    return any(fallback_by_field.get(field) in _SEMANTIC_FALLBACKS for field in original - sent)


def _extract_error_meta(body: object) -> tuple[str, str]:
    if not isinstance(body, dict):
        return "", ""
    error = body.get("error", body)
    if not isinstance(error, dict):
        return "", ""
    raw_code = error.get("code")
    raw_type = error.get("type")
    code = raw_code if isinstance(raw_code, str) else ""
    err_type = raw_type if isinstance(raw_type, str) else ""
    return code, err_type


def guess_dialect(base_url: str, model: str = "") -> Dialect:
    host = (urlparse(base_url).hostname or "").lower()
    model_lower = model.lower()
    if host == "nvidia.com" or host.endswith(".nvidia.com") or model_lower.startswith("nvidia/nemotron"):
        if "120b" in model_lower or "super-vl" in model_lower:
            return "nemotron_off"
        if "ultra" in model_lower or "550b" in model_lower:
            return "nemotron"
        return "generic"
    if host.endswith("deepseek.com"):
        return "deepseek"
    if host.endswith("openai.com"):
        return "openai"
    return "generic"


def _make_llm_request_hook(*, allow_private: bool, resolve_dns: bool = True):
    async def _guard(request: httpx2.Request) -> None:
        if allow_private:
            return
        try:
            validate_outbound_request_url(
                str(request.url),
                allow_private=False,
                label="翻译服务地址",
                resolve_dns=resolve_dns,
            )
        except ValueError as exc:
            raise ChatError(str(exc), retryable=False) from None

    return _guard


class OpenAICompatChat:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dialect: Dialect | None = None,
        timeout: float = 600,
        call_timeout_s: float | None = None,
        client: AsyncOpenAI | None = None,
        disable_redirects: bool = True,
        default_max_retries: int = 3,
        allow_private: bool = False,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ChatError("未配置翻译服务的 API Key")
        self.model = model
        self.dialect: Dialect = dialect or guess_dialect(base_url, model)
        self.provider_id = f"openai:{self.dialect}:{base_url.rstrip('/')}"
        self._disabled: set[str] = set()
        self._default_max_retries = max(0, default_max_retries)
        self._timeout = float(timeout)
        self.call_timeout_s: float | None = float(call_timeout_s) if call_timeout_s is not None else None
        self.total_timeout_s: float | None = self.call_timeout_s
        self._http_client: httpx2.AsyncClient | None = None
        if client is not None:
            self._client = client
        else:
            hooks = (
                {"request": [_make_llm_request_hook(allow_private=allow_private, resolve_dns=transport is None)]}
                if not allow_private
                else None
            )
            effective_transport = (
                transport if transport is not None else build_safe_async_transport(allow_private=allow_private)
            )
            self._http_client = httpx2.AsyncClient(
                follow_redirects=not disable_redirects,
                timeout=timeout,
                transport=effective_transport,
                event_hooks=hooks,
            )
            self._client = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=self._default_max_retries,
                http_client=self._http_client,
            )

    async def aclose(self) -> None:
        try:
            await self._client.close()
        finally:
            if self._http_client is not None and not self._http_client.is_closed:
                await self._http_client.aclose()

    def _base_request_kwargs(self, opts: ChatOptions) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model, "max_tokens": opts.max_tokens}
        if self.dialect == "deepseek":
            if opts.thinking == "off":
                kw["extra_body"] = {"thinking": {"type": "disabled"}}
                kw["temperature"] = opts.temperature
            else:
                kw["extra_body"] = {"thinking": {"type": "enabled"}}
                kw["reasoning_effort"] = opts.thinking
        elif self.dialect == "openai":
            kw["reasoning_effort"] = "none" if opts.thinking == "off" else opts.thinking
        elif self.dialect == "nemotron_off":
            kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            kw["temperature"] = opts.temperature
        elif self.dialect == "nemotron":
            if opts.thinking == "off":
                kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
                kw["temperature"] = opts.temperature
            else:
                kw["reasoning_effort"] = opts.thinking
        else:
            kw["temperature"] = opts.temperature
        return kw

    def request_kwargs(self, opts: ChatOptions) -> dict[str, Any]:
        kw = self._base_request_kwargs(opts)
        if "use_max_completion_tokens" in self._disabled:
            kw["max_completion_tokens"] = kw.pop("max_tokens")
        if "drop_thinking" in self._disabled and self.dialect != "nemotron_off":
            kw.pop("extra_body", None)
        if "drop_reasoning_effort" in self._disabled:
            kw.pop("reasoning_effort", None)
        if "drop_temperature" in self._disabled:
            kw.pop("temperature", None)
        return kw

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        original_fields = _sent_fields(self._base_request_kwargs(opts))
        sent = self.request_kwargs(opts)
        effective_retries = min(opts.transport_retries, self._default_max_retries)
        try:
            try:
                r = await self._client.with_options(max_retries=effective_retries).chat.completions.create(
                    messages=messages, **sent
                )
            except openai.APIStatusError as e:
                if e.status_code != 400:
                    raise
                fields = _incompatible_fields(e.body, _sent_fields(sent) & _FALLBACK_BY_FIELD.keys())
                if not fields:
                    raise
                delta = {_FALLBACK_BY_FIELD[field] for field in fields}
                if "thinking" in fields:
                    delta.add("drop_reasoning_effort")
                if self.dialect == "nemotron_off":
                    delta.discard("drop_thinking")
                if not delta:
                    raise
                self._disabled |= delta
                next_kw = self.request_kwargs(opts)
                if next_kw == sent:
                    raise
                log.info("兼容降级 protocol=openai flags=%s status=%d", ",".join(sorted(delta)), e.status_code)
                sent = next_kw
                r = await self._client.with_options(max_retries=0).chat.completions.create(messages=messages, **sent)
        except openai.AuthenticationError as e:
            code, _ = _extract_error_meta(e.body)
            raise ChatError("翻译服务的 API Key 无效", status_code=e.status_code, error_code=code) from e
        except openai.RateLimitError as e:
            # 兼容服务也会用 429 表示欠费；普通通道只有明确限流码才重试，NIM 通道排除欠费/停用后允许重试。
            code, err_type = _extract_error_meta(e.body)
            if self.dialect in ("nemotron", "nemotron_off"):
                limited = (
                    code.casefold() not in _NON_RETRYABLE_429_CODES
                    and err_type.casefold() not in _NON_RETRYABLE_429_CODES
                )
            else:
                limited = code == "rate_limit_exceeded"
            raise ChatError(
                "翻译服务请求过于频繁或余额不足，请稍后再试",
                retryable=limited,
                status_code=429,
                error_code=code,
            ) from e
        except openai.APIConnectionError as e:
            raise ChatError("无法连接翻译服务，请检查 Base URL 与网络", retryable=True) from e
        except openai.APIStatusError as e:
            code, _ = _extract_error_meta(e.body)
            retryable = e.status_code in (408, 409, 425) or e.status_code >= 500
            raise ChatError(
                f"翻译服务返回错误：HTTP {e.status_code}",
                retryable=retryable,
                status_code=e.status_code,
                error_code=code,
            ) from None
        choice = r.choices[0]
        return ChatResult(
            text=choice.message.content or "",
            finish_reason="refusal" if choice.message.refusal else choice.finish_reason,
            usage=_usage(r.usage),
            compatibility_fallback=_semantic_fallback(original_fields, _sent_fields(sent), _FALLBACK_BY_FIELD),
        )

    async def list_models(self) -> list[str]:
        try:
            # 配置面板不能沿用翻译的长超时；总预算也覆盖分页，避免网关先返回 504。
            async with asyncio.timeout(MODEL_LIST_TIMEOUT_S):
                client = self._client.with_options(max_retries=0, timeout=MODEL_LIST_TIMEOUT_S)
                ids: set[str] = set()
                first = await client.models.list()
                async for page in first.iter_pages():
                    if not isinstance(page.data, list):
                        raise ChatError("服务返回的模型列表格式不正确，请手动填写模型名")
                    for model in page.data:
                        if not isinstance(model.id, str) or not model.id.strip():
                            raise ChatError("服务返回的模型列表格式不正确，请手动填写模型名")
                        ids.add(model.id.strip())
                return sorted(ids)
        except (TimeoutError, openai.APITimeoutError) as e:
            raise ChatError("获取模型列表超时，请重试或手动填写模型名") from e
        except openai.AuthenticationError as e:
            code, _ = _extract_error_meta(e.body)
            raise ChatError("翻译服务的 API Key 无效", status_code=e.status_code, error_code=code) from e
        except openai.PermissionDeniedError as e:
            code, _ = _extract_error_meta(e.body)
            raise ChatError(
                "这个 Key 没有查询模型列表的权限，请手动填写模型名",
                status_code=e.status_code,
                error_code=code,
            ) from e
        except openai.RateLimitError as e:
            code, _ = _extract_error_meta(e.body)
            raise ChatError(
                "模型列表请求受限，请稍后重试或手动填写模型名",
                status_code=429,
                error_code=code,
            ) from e
        except openai.APIConnectionError as e:
            raise ChatError("无法连接翻译服务，请检查 Base URL 与网络") from e
        except openai.APIStatusError as e:
            code, _ = _extract_error_meta(e.body)
            raise ChatError(
                f"这个服务不提供模型列表（HTTP {e.status_code}），请手动填写模型名",
                status_code=e.status_code,
                error_code=code,
            ) from e
        except (openai.APIResponseValidationError, AttributeError, TypeError, ValueError) as e:
            raise ChatError("服务返回的模型列表格式不正确，请手动填写模型名") from e


MAX_CHAIN_BUDGET_S = 180.0


class FallbackChat:
    """轻量免费通道容灾包装器：主模型与备选共享有界整链截止时间，避免逐个上游无限累加等待。"""

    def __init__(
        self,
        primary: ChatModel,
        fallbacks: Sequence[ChatModel],
        *,
        primary_timeout_s: float | None = None,
        total_timeout_s: float | None = None,
        max_chain_budget_s: float = MAX_CHAIN_BUDGET_S,
    ) -> None:
        self._primary = primary
        self._fallbacks = tuple(fallbacks)
        self.model = primary.model
        self.provider_id = str(getattr(primary, "provider_id", ""))
        primary_attr = getattr(primary, "call_timeout_s", None)
        self.primary_timeout_s: float | None = (
            float(primary_timeout_s)
            if primary_timeout_s is not None
            else (float(primary_attr) if isinstance(primary_attr, (int, float)) and primary_attr > 0 else None)
        )
        if total_timeout_s is not None:
            self.total_timeout_s: float | None = float(total_timeout_s)
        else:
            member_timeouts: list[float] = []
            if self.primary_timeout_s is not None:
                member_timeouts.append(self.primary_timeout_s)
            for fb in self._fallbacks:
                fb_attr = getattr(fb, "call_timeout_s", None)
                if isinstance(fb_attr, (int, float)) and fb_attr > 0:
                    member_timeouts.append(float(fb_attr))
                elif self.primary_timeout_s is not None:
                    member_timeouts.append(self.primary_timeout_s)
            self.total_timeout_s = min(sum(member_timeouts), float(max_chain_budget_s)) if member_timeouts else None
        self.call_timeout_s: float | None = self.total_timeout_s

    @property
    def has_fallbacks(self) -> bool:
        return bool(self._fallbacks)

    async def complete(
        self,
        system: str,
        user: str,
        opts: ChatOptions,
        *,
        primary_timeout_s: float | None = None,
        total_timeout_s: float | None = None,
    ) -> ChatResult:
        chain_budget = total_timeout_s if total_timeout_s is not None else self.total_timeout_s
        deadline = time.monotonic() + float(chain_budget) if chain_budget is not None else None
        slice_timeout = primary_timeout_s if primary_timeout_s is not None else self.primary_timeout_s

        def _attempt_budget(cap: float | None) -> float | None:
            if deadline is None:
                return cap
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 0.0
            return min(cap, remaining) if cap is not None else remaining

        try:
            primary_budget = _attempt_budget(slice_timeout)
            if primary_budget is not None:
                if primary_budget <= 0:
                    raise ChatError("免费通道调用总时限耗尽", retryable=True)
                try:
                    return await asyncio.wait_for(self._primary.complete(system, user, opts), primary_budget)
                except TimeoutError as exc:
                    raise ChatError("主模型响应超时", retryable=True) from exc
            return await self._primary.complete(system, user, opts)
        except ChatError as primary_err:
            should_fallback = bool(self._fallbacks) and (
                primary_err.status_code == 429
                or (primary_err.status_code is not None and primary_err.status_code >= 500)
                or primary_err.retryable
            )
            if not should_fallback:
                raise
            last_err: ChatError = primary_err
            for fb in self._fallbacks:
                fb_attr = getattr(fb, "call_timeout_s", None)
                fb_cap = float(fb_attr) if isinstance(fb_attr, (int, float)) and fb_attr > 0 else None
                fb_budget = _attempt_budget(fb_cap)
                if fb_budget is not None and fb_budget <= 0:
                    last_err = ChatError("免费通道调用总时限耗尽", retryable=True)
                    break
                try:
                    if fb_budget is not None:
                        result = await asyncio.wait_for(fb.complete(system, user, opts), fb_budget)
                    else:
                        result = await fb.complete(system, user, opts)
                except TimeoutError:
                    log.warning("备选模型调用超时 model=%s", fb.model)
                    last_err = ChatError("备选模型响应超时", retryable=True)
                    continue
                except ChatError as fb_err:
                    log.warning("备选模型调用失败 model=%s status=%s", fb.model, fb_err.status_code)
                    last_err = fb_err
                    continue
                return result.model_copy(
                    update={
                        "actual_model": result.actual_model or fb.model,
                        "used_fallback": True,
                    }
                )
            raise last_err from primary_err

    async def list_models(self) -> list[str]:
        return await self._primary.list_models()

    async def aclose(self) -> None:
        clients: list[Any] = [self._primary, *self._fallbacks]
        first_error: BaseException | None = None
        for client in clients:
            closer = getattr(client, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def _usage(u: Any) -> ChatUsage:
    if u is None:
        return ChatUsage()
    data = u.model_dump()
    hit = data.get("prompt_cache_hit_tokens")  # DeepSeek
    if hit is None:
        hit = (data.get("prompt_tokens_details") or {}).get("cached_tokens") or 0  # OpenAI
    reasoning = (data.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    return ChatUsage(
        prompt_tokens=data.get("prompt_tokens") or 0,
        cache_hit_tokens=hit,
        completion_tokens=data.get("completion_tokens") or 0,
        reasoning_tokens=reasoning,
    )
