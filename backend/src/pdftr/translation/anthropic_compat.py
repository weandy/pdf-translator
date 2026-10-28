"""Anthropic Messages 协议适配器（Claude 官方接口，以及提供 Anthropic 兼容接口的服务，如 DeepSeek /anthropic）。

思考开关只对认得的 Claude 新模型（4.6 及以后）发送：
- 默认档：output_config.effort = low（Claude Opus 5.5 等新模型不能关闭思考，只能调低强度）
- 精翻：thinking = adaptive，effort = medium
其他模型（含第三方兼容接口）不发送思考参数，交给接口默认，避免发了不认识的参数报错。
新版接口（2026-10-21 起）不再接受 temperature，因此也不发送。

anthropic SDK 只在选用这个协议时才导入，默认的 OpenAI 兼容协议不占这部分内存。
"""

import re
from typing import TYPE_CHECKING, Any

from pdftr.domain.llm import ChatError, ChatOptions, ChatResult, ChatUsage

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

_EFFORT_MODELS = re.compile(r"^claude-[a-z]+-(4-[6-9]|[5-9])")  # 支持 output_config.effort 与 adaptive 思考


class AnthropicChat:
    def __init__(
        self, *, base_url: str, api_key: str, model: str, timeout: float = 600, client: "AsyncAnthropic | None" = None
    ) -> None:
        if not api_key:
            raise ChatError("未配置翻译服务的 API Key")
        from anthropic import AsyncAnthropic

        self.model = model
        # SDK 2.x 用 Authorization: Bearer 传 Key；兼容接口（如 DeepSeek /anthropic）多数只认 x-api-key，两个都带上
        self._client = client or AsyncAnthropic(
            base_url=base_url, api_key=api_key, timeout=timeout, max_retries=3, default_headers={"x-api-key": api_key}
        )

    def request_kwargs(self, opts: ChatOptions) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model, "max_tokens": opts.max_tokens}
        if _EFFORT_MODELS.match(self.model):
            if opts.thinking == "off":
                kw["output_config"] = {"effort": "low"}
            else:
                kw["thinking"] = {"type": "adaptive"}
                kw["output_config"] = {"effort": "medium"}
        return kw

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        import anthropic

        try:
            r = await self._client.messages.create(
                system=system, messages=[{"role": "user", "content": user}], **self.request_kwargs(opts)
            )
        except anthropic.AuthenticationError as e:
            raise ChatError("翻译服务的 API Key 无效") from e
        except anthropic.PermissionDeniedError as e:
            raise ChatError("翻译服务拒绝访问：这个 Key 没有使用该模型的权限") from e
        except anthropic.NotFoundError as e:
            raise ChatError("翻译服务找不到这个模型，请检查模型名与接口地址") from e
        except anthropic.RateLimitError as e:
            raise ChatError("翻译服务请求过于频繁或余额不足，请稍后再试") from e
        except anthropic.APIConnectionError as e:
            raise ChatError("无法连接翻译服务，请检查接口地址与网络") from e
        except anthropic.APIStatusError as e:
            raise ChatError(f"翻译服务返回错误：HTTP {e.status_code}") from e
        # 模型拒绝（stop_reason=refusal）时没有正文，这批段落按格式错误处理、标为失败，不影响整个任务
        text = "".join(b.text for b in r.content if b.type == "text")
        u = r.usage
        hit = u.cache_read_input_tokens or 0
        prompt = u.input_tokens + hit + (u.cache_creation_input_tokens or 0)
        return ChatResult(
            text=text, usage=ChatUsage(prompt_tokens=prompt, cache_hit_tokens=hit, completion_tokens=u.output_tokens)
        )

    async def list_models(self) -> list[str]:
        import anthropic

        try:
            return [m.id async for m in self._client.models.list(limit=100)]
        except anthropic.AuthenticationError as e:
            raise ChatError("翻译服务的 API Key 无效") from e
        except anthropic.APIConnectionError as e:
            raise ChatError("无法连接翻译服务，请检查接口地址与网络") from e
        except anthropic.APIStatusError as e:
            raise ChatError(f"这个服务不提供模型列表（HTTP {e.status_code}），请手动填写模型名") from e
