"""Anthropic Messages 协议适配器（Claude 官方接口，以及提供 Anthropic 兼容接口的服务，如 DeepSeek /anthropic）。

思考开关只对认得的 Claude 新模型（4.6 及以后）发送：
- 默认档：output_config.effort = low（Claude Opus 5.5 等新模型不能关闭思考，只能调低强度）
- 精翻：thinking = adaptive，effort = medium
其他模型（含第三方兼容接口）不发送思考参数，交给接口默认，避免发了不认识的参数报错。
新版接口（2026-10-21 起）不再接受 temperature，因此也不发送。

anthropic SDK 只在选用这个协议时才导入，默认的 OpenAI 兼容协议不占这部分内存。
"""

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from pdftr.domain.llm import ChatError, ChatOptions, ChatResult, ChatUsage
from pdftr.translation.openai_compat import _incompatible_fields, _semantic_fallback

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from anthropic.types import MessageParam

_EFFORT_MODELS = re.compile(r"^claude-(?:opus|sonnet|haiku)-(?:4[.-][6-9]|[5-9](?:[.-]\d+)?)(?:$|-)")
_FALLBACK_BY_FIELD = {"output_config": "drop_output_config", "thinking": "drop_thinking"}
MODEL_LIST_TIMEOUT_S = 15
log = logging.getLogger(__name__)


class AnthropicChat:
    def __init__(
        self, *, base_url: str, api_key: str, model: str, timeout: float = 600, client: "AsyncAnthropic | None" = None
    ) -> None:
        if not api_key:
            raise ChatError("未配置翻译服务的 API Key")
        from anthropic import AsyncAnthropic

        self.model = model
        self.provider_id = f"anthropic:{base_url.rstrip('/')}"
        self._disabled: set[str] = set()
        # SDK 2.x 用 Authorization: Bearer 传 Key；兼容接口（如 DeepSeek /anthropic）多数只认 x-api-key，两个都带上
        self._client = client or AsyncAnthropic(
            base_url=base_url, api_key=api_key, timeout=timeout, max_retries=3, default_headers={"x-api-key": api_key}
        )

    async def aclose(self) -> None:
        await self._client.close()

    def _base_request_kwargs(self, opts: ChatOptions) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model, "max_tokens": opts.max_tokens}
        if _EFFORT_MODELS.match(self.model):
            if opts.thinking == "off":
                kw["output_config"] = {"effort": "low"}
            else:
                kw["thinking"] = {"type": "adaptive"}
                kw["output_config"] = {"effort": "medium"}
        return kw

    def request_kwargs(self, opts: ChatOptions) -> dict[str, Any]:
        kw = self._base_request_kwargs(opts)
        for field, flag in _FALLBACK_BY_FIELD.items():
            if flag in self._disabled:
                kw.pop(field, None)
        return kw

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        import anthropic

        messages: list[MessageParam] = [{"role": "user", "content": user}]
        original_fields = set(self._base_request_kwargs(opts))
        sent = self.request_kwargs(opts)
        try:
            try:
                r = await self._client.with_options(max_retries=opts.transport_retries).messages.create(
                    system=system, messages=messages, **sent
                )
            except anthropic.APIStatusError as e:
                if e.status_code != 400:
                    raise
                fields = _incompatible_fields(
                    e.body, sent.keys() & _FALLBACK_BY_FIELD.keys(), aliases={"output_config": "effort"}
                )
                if not fields:
                    raise
                delta = {_FALLBACK_BY_FIELD[field] for field in fields}
                self._disabled |= delta
                next_kw = self.request_kwargs(opts)
                if next_kw == sent:
                    raise
                log.info("兼容降级 protocol=anthropic flags=%s status=%d", ",".join(sorted(delta)), e.status_code)
                sent = next_kw
                r = await self._client.with_options(max_retries=0).messages.create(
                    system=system, messages=messages, **sent
                )
        except anthropic.AuthenticationError as e:
            raise ChatError("翻译服务的 API Key 无效") from e
        except anthropic.PermissionDeniedError as e:
            raise ChatError("翻译服务拒绝访问：这个 Key 没有使用该模型的权限") from e
        except anthropic.NotFoundError as e:
            raise ChatError("翻译服务找不到这个模型，请检查模型名与接口地址") from e
        except anthropic.RateLimitError as e:
            body = e.body if isinstance(e.body, dict) else {}
            error = body.get("error", body)
            limited = isinstance(error, dict) and error.get("type") == "rate_limit_error"
            raise ChatError("翻译服务请求过于频繁或余额不足，请稍后再试", retryable=limited) from e
        except anthropic.APIConnectionError as e:
            raise ChatError("无法连接翻译服务，请检查接口地址与网络", retryable=True) from e
        except anthropic.APIStatusError as e:
            retryable = e.status_code in (408, 409, 425) or e.status_code >= 500
            raise ChatError(f"翻译服务返回错误：HTTP {e.status_code}", retryable=retryable) from None
        text = "".join(b.text for b in r.content if b.type == "text")
        u = r.usage
        hit = u.cache_read_input_tokens or 0
        prompt = u.input_tokens + hit + (u.cache_creation_input_tokens or 0)
        return ChatResult(
            text=text,
            finish_reason=r.stop_reason,
            usage=ChatUsage(prompt_tokens=prompt, cache_hit_tokens=hit, completion_tokens=u.output_tokens),
            compatibility_fallback=_semantic_fallback(original_fields, set(sent), _FALLBACK_BY_FIELD),
        )

    async def list_models(self) -> list[str]:
        import anthropic

        try:
            # 查询模型应快速结束；分页与网络等待共用总预算，且不做 SDK 隐藏重试。
            async with asyncio.timeout(MODEL_LIST_TIMEOUT_S):
                client = self._client.with_options(max_retries=0, timeout=MODEL_LIST_TIMEOUT_S)
                ids: set[str] = set()
                first = await client.models.list(limit=100)
                async for page in first.iter_pages():
                    if not isinstance(page.data, list):
                        raise ChatError("服务返回的模型列表格式不正确，请手动填写模型名")
                    for model in page.data:
                        if not isinstance(model.id, str) or not model.id.strip():
                            raise ChatError("服务返回的模型列表格式不正确，请手动填写模型名")
                        ids.add(model.id.strip())
                return sorted(ids)
        except (TimeoutError, anthropic.APITimeoutError) as e:
            raise ChatError("获取模型列表超时，请重试或手动填写模型名") from e
        except anthropic.AuthenticationError as e:
            raise ChatError("翻译服务的 API Key 无效") from e
        except anthropic.PermissionDeniedError as e:
            raise ChatError("这个 Key 没有查询模型列表的权限，请手动填写模型名") from e
        except anthropic.RateLimitError as e:
            raise ChatError("模型列表请求受限，请稍后重试或手动填写模型名") from e
        except anthropic.APIConnectionError as e:
            raise ChatError("无法连接翻译服务，请检查接口地址与网络") from e
        except anthropic.APIStatusError as e:
            raise ChatError(f"这个服务不提供模型列表（HTTP {e.status_code}），请手动填写模型名") from e
        except (anthropic.APIResponseValidationError, AttributeError, TypeError, ValueError) as e:
            raise ChatError("服务返回的模型列表格式不正确，请手动填写模型名") from e
