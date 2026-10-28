"""大模型端口：翻译与术语抽取都通过它调用模型。

放在 domain，因为 translation 和 glossary 两个能力模块都要用，而它们之间不能互相导入。
OpenAI 兼容接口、各家思考开关的差异由适配器（translation/openai_compat.py）处理。
"""

from typing import Literal, Protocol

from pydantic import BaseModel

Thinking = Literal["off", "low"]  # S8–S11：默认不思考，“精翻”用 low


class ChatOptions(BaseModel):
    temperature: float = 0.3  # S9：学术公式文本用 0.3 比官方建议的 1.3 更稳
    max_tokens: int = 16000
    thinking: Thinking = "off"


class ChatUsage(BaseModel):
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def cache_miss_tokens(self) -> int:
        return max(0, self.prompt_tokens - self.cache_hit_tokens)

    def __add__(self, other: "ChatUsage") -> "ChatUsage":
        return ChatUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


class ChatResult(BaseModel):
    text: str
    usage: ChatUsage = ChatUsage()


class ChatError(RuntimeError):
    """调用大模型失败（鉴权、额度、网络等），消息面向用户。"""


class ChatModel(Protocol):
    model: str

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult: ...
