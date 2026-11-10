"""OpenAI 兼容接口适配器。

各家“思考”开关的写法不同（参考 zotero-pdf-translate、KISS Translator 的做法）：
- deepseek：extra_body.thinking = {"type": "enabled"|"disabled"}，开启时 reasoning_effort=low
- openai：reasoning_effort = "none"|"low"，推理模型不接受 temperature
- generic：不发送任何思考参数，交给接口默认（未知服务发了不认识的参数可能报错）
"""

import asyncio
from typing import Any, Literal
from urllib.parse import urlparse

import openai
from openai import AsyncOpenAI

from pdftr.domain.llm import ChatError, ChatOptions, ChatResult, ChatUsage

Dialect = Literal["deepseek", "openai", "generic"]
MODEL_LIST_TIMEOUT_S = 15


def guess_dialect(base_url: str) -> Dialect:
    host = urlparse(base_url).hostname or ""
    if host.endswith("deepseek.com"):
        return "deepseek"
    if host.endswith("openai.com"):
        return "openai"
    return "generic"


class OpenAICompatChat:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dialect: Dialect | None = None,
        timeout: float = 600,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if not api_key:
            raise ChatError("未配置翻译服务的 API Key")
        self.model = model
        self.dialect: Dialect = dialect or guess_dialect(base_url)
        self._client = client or AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=3)

    def request_kwargs(self, opts: ChatOptions) -> dict[str, Any]:
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
        else:
            kw["temperature"] = opts.temperature
        return kw

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        try:
            r = await self._client.with_options(max_retries=opts.transport_retries).chat.completions.create(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **self.request_kwargs(opts),
            )
        except openai.AuthenticationError as e:
            raise ChatError("翻译服务的 API Key 无效") from e
        except openai.RateLimitError as e:
            # 兼容服务也会用 429 表示欠费；只有明确的限流代码才允许自动重试。
            body = e.body if isinstance(e.body, dict) else {}
            error = body.get("error", body)
            limited = isinstance(error, dict) and error.get("code") == "rate_limit_exceeded"
            raise ChatError("翻译服务请求过于频繁或余额不足，请稍后再试", retryable=limited) from e
        except openai.APIConnectionError as e:
            raise ChatError("无法连接翻译服务，请检查 Base URL 与网络", retryable=True) from e
        except openai.APIStatusError as e:
            retryable = e.status_code in (408, 409, 425) or e.status_code >= 500
            raise ChatError(f"翻译服务返回错误：HTTP {e.status_code}", retryable=retryable) from e
        return ChatResult(text=r.choices[0].message.content or "", usage=_usage(r.usage))

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
            raise ChatError("翻译服务的 API Key 无效") from e
        except openai.PermissionDeniedError as e:
            raise ChatError("这个 Key 没有查询模型列表的权限，请手动填写模型名") from e
        except openai.RateLimitError as e:
            raise ChatError("模型列表请求受限，请稍后重试或手动填写模型名") from e
        except openai.APIConnectionError as e:
            raise ChatError("无法连接翻译服务，请检查 Base URL 与网络") from e
        except openai.APIStatusError as e:
            raise ChatError(f"这个服务不提供模型列表（HTTP {e.status_code}），请手动填写模型名") from e
        except (openai.APIResponseValidationError, AttributeError, TypeError, ValueError) as e:
            raise ChatError("服务返回的模型列表格式不正确，请手动填写模型名") from e


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
