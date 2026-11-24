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


def _chat(
    dialect,
    status: int = 200,
    error: dict | None = None,
    *,
    finish_reason: str = "stop",
    replies: tuple[tuple[int, dict | None], ...] = (),
    model: str = "m",
) -> tuple[OpenAICompatChat, list[dict]]:
    seen: list[dict] = []
    pending = iter(replies)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        reply_status, reply_error = next(pending, (status, error))
        if reply_status != 200:
            return httpx2.Response(
                reply_status, json={"error": reply_error or {"message": "x"}}, headers={"retry-after-ms": "1"}
            )
        return httpx2.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [
                {"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": "1|译文"}}
            ],
            "usage": USAGE_DEEPSEEK,
        })  # fmt: skip

    client = AsyncOpenAI(
        base_url="https://api.example/v1", api_key="k", max_retries=3,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )  # fmt: skip
    return OpenAICompatChat(
        base_url="https://api.example/v1/", api_key="k", model=model, dialect=dialect, client=client
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


def test_provider_fingerprint_and_generic_model_name():
    chat, seen = _chat("generic", model="deepseek-reasoner")
    official, _ = _chat("deepseek", model=chat.model)
    assert chat.provider_id == "openai:generic:https://api.example/v1"
    assert official.provider_id == "openai:deepseek:https://api.example/v1"
    result = asyncio.run(chat.complete("s", "u", ChatOptions(thinking="low")))
    assert not ({"reasoning_effort", "thinking"} & seen[0].keys())
    assert seen[0]["temperature"] == 0.3 and not result.compatibility_fallback


@pytest.mark.parametrize(
    ("error", "removed", "semantic"),
    [
        ({"message": "Use max_completion_tokens instead of max_tokens"}, {"max_tokens"}, False),
        ({"param": "reasoning_effort"}, {"reasoning_effort"}, True),
        ({"message": "Unknown extra_body.thinking"}, {"thinking", "reasoning_effort"}, True),
    ],
)
def test_confirmed_400_fallback_is_remembered(error, removed, semantic):
    chat, seen = _chat("deepseek", replies=((400, error),))
    opts = ChatOptions(thinking="low")

    async def call_twice():
        return [await chat.complete("s", "u", opts) for _ in range(2)]

    results = asyncio.run(call_twice())
    assert len(seen) == 3 and seen[1] == seen[2]
    assert seen[0].keys() - seen[1].keys() == removed
    assert all(result.compatibility_fallback is semantic for result in results)
    if not semantic:
        assert seen[1]["max_completion_tokens"] == seen[0]["max_tokens"]


def test_sampling_fallback_only_marks_calls_that_would_send_temperature(caplog):
    error = {"message": "Unsupported temperature. synthetic-remote-detail"}
    chat, seen = _chat("deepseek", replies=((400, error),))

    async def call_both():
        return [await chat.complete("s", "u", ChatOptions(thinking=thinking)) for thinking in ("off", "low", "off")]

    with caplog.at_level("INFO", logger="pdftr.translation.openai_compat"):
        results = asyncio.run(call_both())
    assert [r.compatibility_fallback for r in results] == [True, False, True]
    assert len(seen) == 4 and all("temperature" not in body for body in seen[1:])
    assert "protocol=openai flags=drop_temperature status=400" in caplog.text
    assert "synthetic-remote-detail" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        {"param": "thinking", "message": "unknown field thinking"},
        {"param": "temperature", "code": "context_length_exceeded"},
        {"message": "prompt is too long; invalid max_tokens"},
        {"message": "unexpected max_tokens_extra"},
        {"message": "unknown unsupported max_completion_tokens"},
        {"message": "info about temperature"},
    ],
)
def test_400_ignores_unsent_fields_context_limits_and_inexact_signals(error):
    chat, seen = _chat("generic", status=400, error=error)
    opts = ChatOptions()
    with pytest.raises(ChatError, match="HTTP 400") as caught:
        asyncio.run(chat.complete("s", "u", opts))
    assert not caught.value.retryable and len(seen) == 1
    assert chat.request_kwargs(opts) == {"model": "m", "max_tokens": opts.max_tokens, "temperature": opts.temperature}


@pytest.mark.parametrize("status", [400, 503])
def test_fallback_request_has_no_sdk_retries_or_second_downgrade(status):
    chat, seen = _chat(
        "deepseek",
        status=status,
        error={"param": "temperature"},
        replies=((400, {"param": "thinking"}),),
    )
    with pytest.raises(ChatError, match=f"HTTP {status}"):
        asyncio.run(chat.complete("s", "u", ChatOptions()))
    assert len(seen) == 2
    assert "thinking" not in seen[1] and "temperature" in chat.request_kwargs(ChatOptions())


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


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_model_finish_reason(reason):
    chat, _ = _chat("generic", finish_reason=reason)
    result = asyncio.run(chat.complete("s", "u", ChatOptions()))
    assert result.finish_reason == reason
