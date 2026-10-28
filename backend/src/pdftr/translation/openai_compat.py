"""OpenAI 兼容接口适配器。

各家“思考”开关的写法不同（参考 zotero-pdf-translate、KISS Translator 的做法）：
- deepseek：extra_body.thinking = {"type": "enabled"|"disabled"}，开启时 reasoning_effort=low
- openai：reasoning_effort = "none"|"low"，推理模型不接受 temperature
- generic：不发送任何思考参数，交给接口默认（未知服务发了不认识的参数可能报错）
"""

from typing import Any, Literal
from urllib.parse import urlparse

import openai
from openai import AsyncOpenAI

from pdftr.domain.llm import ChatError, ChatOptions, ChatResult, ChatUsage

Dialect = Literal["deepseek", "openai", "generic"]


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
            r = await self._client.chat.completions.create(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **self.request_kwargs(opts),
            )
        except openai.AuthenticationError as e:
            raise ChatError("翻译服务的 API Key 无效") from e
        except openai.RateLimitError as e:
            raise ChatError("翻译服务请求过于频繁或余额不足，请稍后再试") from e
        except openai.APIConnectionError as e:
            raise ChatError("无法连接翻译服务，请检查 Base URL 与网络") from e
        except openai.APIStatusError as e:
            raise ChatError(f"翻译服务返回错误：HTTP {e.status_code}") from e
        return ChatResult(text=r.choices[0].message.content or "", usage=_usage(r.usage))


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
