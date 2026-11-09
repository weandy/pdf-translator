"""OpenAI 兼容适配器：检查发给各家的请求体（思考开关方言）与用量解析，不访问网络。"""

import asyncio
import json

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from pdftr.domain.llm import ChatError, ChatOptions
from pdftr.translation.openai_compat import OpenAICompatChat, guess_dialect

USAGE_DEEPSEEK = {
    "prompt_tokens": 1540,
    "completion_tokens": 1131,
    "total_tokens": 2671,
    "prompt_cache_hit_tokens": 512,
    "prompt_cache_miss_tokens": 1028,
    "completion_tokens_details": {"reasoning_tokens": 900},
}


def _chat(dialect, status: int = 200, error: dict | None = None) -> tuple[OpenAICompatChat, list[dict]]:
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        if status != 200:
            return httpx2.Response(status, json={"error": error or {"message": "x"}}, headers={"retry-after-ms": "1"})
        return httpx2.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "1|译文"}}],
            "usage": USAGE_DEEPSEEK,
        })  # fmt: skip

    client = AsyncOpenAI(
        base_url="https://api.example/v1", api_key="k", max_retries=3,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )  # fmt: skip
    return OpenAICompatChat(
        base_url="https://api.example/v1", api_key="k", model="m", dialect=dialect, client=client
    ), seen


def test_guess_dialect():
    assert guess_dialect("https://api.deepseek.com") == "deepseek"
    assert guess_dialect("https://api.openai.com/v1") == "openai"
    assert guess_dialect("http://localhost:11434/v1") == "generic"


@pytest.mark.parametrize(
    ("dialect", "thinking", "expect", "absent"),
    [
        ("deepseek", "off", {"thinking": {"type": "disabled"}, "temperature": 0.3}, ["reasoning_effort"]),
        ("deepseek", "low", {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}, ["temperature"]),
        ("openai", "off", {"reasoning_effort": "none"}, ["temperature", "thinking"]),
        ("generic", "low", {"temperature": 0.3}, ["thinking", "reasoning_effort"]),
    ],
)
def test_request_body(dialect, thinking, expect, absent):
    chat, seen = _chat(dialect)
    result = asyncio.run(chat.complete("sys", "1|text", ChatOptions(thinking=thinking)))
    body = seen[0]
    for k, v in expect.items():
        assert body[k] == v
    for k in absent:
        assert k not in body
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert result.text == "1|译文"
    assert result.usage.cache_hit_tokens == 512 and result.usage.cache_miss_tokens == 1028
    assert result.usage.reasoning_tokens == 900


@pytest.mark.parametrize(("status", "message"), [(401, "API Key 无效"), (429, "余额不足"), (500, "HTTP 500")])
def test_errors(status, message):
    chat, _ = _chat("deepseek", status=status)
    with pytest.raises(ChatError, match=message):
        asyncio.run(chat.complete("s", "u", ChatOptions(transport_retries=0)))


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (409, True),
        (425, True),
        (500, True),
        (503, True),
    ],
)
def test_one_transport_request_and_status_classification(status, retryable):
    chat, seen = _chat("generic", status=status)
    with pytest.raises(ChatError) as error:
        asyncio.run(chat.complete("s", "u", ChatOptions(transport_retries=0)))
    assert len(seen) == 1  # 底层客户端默认 3 次重试，调用级 0 必须覆盖它。
    assert error.value.retryable is retryable


@pytest.mark.parametrize(
    ("code", "retryable"), [(None, False), ("insufficient_quota", False), ("rate_limit_exceeded", True)]
)
def test_429_only_retries_explicit_rate_limits(code, retryable):
    chat, seen = _chat("generic", status=429, error={"message": "x", "code": code})
    with pytest.raises(ChatError) as error:
        asyncio.run(chat.complete("s", "u", ChatOptions(transport_retries=0)))
    assert len(seen) == 1 and error.value.retryable is retryable


@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ReadTimeout])
def test_network_failures_are_retryable_without_hidden_retries(failure):
    seen = []

    def handler(request):
        seen.append(request)
        raise failure("synthetic failure", request=request)

    client = AsyncOpenAI(
        base_url="https://api.example/v1",
        api_key="k",
        max_retries=3,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    chat = OpenAICompatChat(base_url="https://api.example/v1", api_key="k", model="m", client=client)
    with pytest.raises(ChatError) as error:
        asyncio.run(chat.complete("s", "u", ChatOptions(transport_retries=0)))
    assert len(seen) == 1 and error.value.retryable


def test_default_transport_retries_and_legacy_chat_error():
    assert ChatOptions().transport_retries == 3
    chat, seen = _chat("generic", status=503)
    with pytest.raises(ChatError):
        asyncio.run(chat.complete("s", "u", ChatOptions()))
    assert len(seen) == 4
    error = ChatError("合成错误")
    assert str(error) == "合成错误" and not error.retryable
    for value in (-1, 4):
        with pytest.raises(ValidationError):
            ChatOptions(transport_retries=value)


def test_missing_key():
    with pytest.raises(ChatError, match="未配置"):
        OpenAICompatChat(base_url="https://api.deepseek.com", api_key="", model="m")


def test_list_models():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"
        return httpx2.Response(200, json={"object": "list", "data": [
            {"id": "deepseek-v4-pro", "object": "model", "created": 0, "owned_by": "x"},
            {"id": "deepseek-flash", "object": "model", "created": 0, "owned_by": "x"},
        ]})  # fmt: skip

    client = AsyncOpenAI(
        base_url="https://api.example/v1", api_key="k", max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )  # fmt: skip
    chat = OpenAICompatChat(base_url="https://api.example/v1", api_key="k", model="m", client=client)
    assert asyncio.run(chat.list_models()) == ["deepseek-flash", "deepseek-v4-pro"]
