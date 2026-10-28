"""大模型端口。

翻译用例只依赖 ChatModel；OpenAI 兼容接口、各家思考开关的差异由适配器处理。
"""

from typing import Literal, Protocol

from pydantic import BaseModel


class ChatOptions(BaseModel):
    model: str
    temperature: float = 0.3  # S9：学术公式文本用 0.3 比官方建议的 1.3 更稳
    max_tokens: int = 16000
    thinking: Literal["off", "low"] = "off"  # S8–S11：默认不思考，“精翻”用 low


class ChatUsage(BaseModel):
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0


class ChatResult(BaseModel):
    text: str
    usage: ChatUsage = ChatUsage()


class ChatModel(Protocol):
    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult: ...
