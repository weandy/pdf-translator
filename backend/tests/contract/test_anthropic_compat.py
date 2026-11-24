"""Anthropic 协议适配器：检查请求体（思考参数只发给认得的 Claude 新模型）、用量解析、模型列表，不访问网络。"""

import asyncio
import json

import httpx2
import pytest
from anthropic import AsyncAnthropic

from pdftr.domain.llm import ChatError, ChatOptions
from pdftr.translation.anthropic_compat import AnthropicChat


def _chat(
    model: str,
    status: int = 200,
    error: dict | None = None,
    *,
    stop_reason: str = "end_turn",
    replies: tuple[tuple[int, dict | None], ...] = (),
) -> tuple[AnthropicChat, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []
    pending = iter(replies)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        reply_status, reply_error = next(pending, (status, error))
        if reply_status != 200:
            return httpx2.Response(
                reply_status,
                json={"type": "error", "error": reply_error or {"type": "x", "message": "x"}},
                headers={"retry-after-ms": "1"},
            )
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={
                "data": [{"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                          "created_at": "2026-01-01T00:00:00Z"}],
                "has_more": False, "first_id": "claude-opus-5-5", "last_id": "claude-opus-5-5",
            })  # fmt: skip
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "1|译文"}],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 400, "cache_creation_input_tokens": 50,
                      "output_tokens": 30},
        })  # fmt: skip

    client = AsyncAnthropic(
        base_url="https://api.example", api_key="k", max_retries=3, default_headers={"x-api-key": "k"},
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )  # fmt: skip
    return AnthropicChat(base_url="https://api.example", api_key="k", model=model, client=client), seen


@pytest.mark.parametrize(
    ("model", "thinking", "expect", "absent"),
    [
        ("claude-opus-5-5", "off", {"output_config": {"effort": "low"}}, ["thinking", "temperature"]),
        ("claude-sonnet-4-6", "low", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}, []),
        ("claude-sonnet-4.6-20260217", "off", {"output_config": {"effort": "low"}}, ["thinking"]),
        # 认不出的模型（第三方兼容接口、旧模型）不发送思考参数
        ("deepseek-flash", "low", {}, ["thinking", "output_config", "temperature"]),
        ("claude-haiku-4-5", "low", {}, ["thinking", "output_config"]),
        ("claude-opus-50", "low", {}, ["thinking", "output_config"]),
        ("claude-sonnet-4-61", "low", {}, ["thinking", "output_config"]),
        ("claude-unknown-5", "low", {}, ["thinking", "output_config"]),
    ],
)
def test_request_body(model, thinking, expect, absent):
    chat, seen = _chat(model)
    r = asyncio.run(chat.complete("系统", "原文", ChatOptions(thinking=thinking)))
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/messages" and seen[0].headers["x-api-key"] == "k"
    assert seen[0].headers["authorization"] == "Bearer k"
    assert body["system"] == "系统" and body["messages"] == [{"role": "user", "content": "原文"}]
    assert all(body[k] == v for k, v in expect.items()) and not any(k in body for k in absent)
    # 只取正文，跳过思考块；输入 token = 未缓存 + 缓存命中 + 写入缓存
    assert r.text == "1|译文"
    assert (r.usage.prompt_tokens, r.usage.cache_hit_tokens, r.usage.completion_tokens) == (550, 400, 30)


@pytest.mark.parametrize(
    ("error", "removed"),
    [
        ({"message": "Effort is not supported"}, "output_config"),
        ({"param": "output_config"}, "output_config"),
        ({"message": "Invalid thinking configuration"}, "thinking"),
    ],
)
def test_400_fallback_only_marks_effective_downgrades(error, removed):
    chat, seen = _chat("claude-sonnet-4-6", replies=((400, error),))

    async def call_both():
        return [await chat.complete("s", "u", ChatOptions(thinking=thinking)) for thinking in ("low", "low", "off")]

    results = asyncio.run(call_both())
    bodies = [json.loads(request.content) for request in seen]
    assert len(bodies) == 4 and bodies[1] == bodies[2]
    assert bodies[0].keys() - bodies[1].keys() == {removed}
    assert [r.compatibility_fallback for r in results] == [True, True, removed == "output_config"]


@pytest.mark.parametrize(
    "error",
    [
        {"param": "thinking", "message": "unsupported thinking"},
        {"param": "output_config", "type": "context_length_exceeded"},
        {"message": "maximum context length reached; invalid output_config"},
        {"message": "invalid output_config_backup and high_effort"},
        {"message": "effort must be measured"},
    ],
)
def test_400_does_not_retry_unsent_fields_context_limits_or_inexact_signals(error):
    chat, seen = _chat("claude-opus-5-5", status=400, error=error)
    opts = ChatOptions()
    with pytest.raises(ChatError, match="HTTP 400") as caught:
        asyncio.run(chat.complete("s", "u", opts))
    assert not caught.value.retryable and len(seen) == 1
    assert chat.request_kwargs(opts)["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("status", [400, 529])
def test_fallback_request_has_no_sdk_retries_or_second_downgrade(status):
    chat, seen = _chat(
        "claude-sonnet-4-6",
        status=status,
        error={"param": "output_config"},
        replies=((400, {"param": "thinking"}),),
    )
    opts = ChatOptions(thinking="low")
    with pytest.raises(ChatError, match=f"HTTP {status}"):
        asyncio.run(chat.complete("s", "u", opts))
    assert len(seen) == 2
    assert "thinking" not in json.loads(seen[1].content) and "output_config" in chat.request_kwargs(opts)


def test_list_models_and_errors():
    chat, _ = _chat("claude-opus-5-5")
    assert asyncio.run(chat.list_models()) == ["claude-opus-5-5"]
    with pytest.raises(ChatError, match="Key 无效"):
        asyncio.run(_chat("m", status=401)[0].complete("s", "u", ChatOptions()))
    with pytest.raises(ChatError, match="手动填写模型名"):
        asyncio.run(_chat("m", status=404)[0].list_models())


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
        (529, True),
    ],
)
def test_one_transport_request_and_status_classification(status, retryable):
    chat, seen = _chat("m", status=status)
    with pytest.raises(ChatError) as error:
        asyncio.run(chat.complete("s", "u", ChatOptions(transport_retries=0)))
    assert len(seen) == 1 and error.value.retryable is retryable


@pytest.mark.parametrize(
    ("kind", "retryable"), [("x", False), ("insufficient_quota", False), ("rate_limit_error", True)]
)
def test_429_only_retries_explicit_rate_limits(kind, retryable):
    chat, seen = _chat("m", status=429, error={"type": kind, "message": "x"})
    with pytest.raises(ChatError) as error:
        asyncio.run(chat.complete("s", "u", ChatOptions(transport_retries=0)))
    assert len(seen) == 1 and error.value.retryable is retryable


@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ReadTimeout])
def test_network_failures_are_retryable_without_hidden_retries(failure):
    seen = []

    def handler(request):
        seen.append(request)
        raise failure("synthetic failure", request=request)

    client = AsyncAnthropic(
        base_url="https://api.example",
        api_key="k",
        max_retries=3,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    chat = AnthropicChat(base_url="https://api.example", api_key="k", model="m", client=client)
    with pytest.raises(ChatError) as error:
        asyncio.run(chat.complete("s", "u", ChatOptions(transport_retries=0)))
    assert len(seen) == 1 and error.value.retryable


def test_default_transport_retries_remain_enabled():
    chat, seen = _chat("m", status=503)
    with pytest.raises(ChatError):
        asyncio.run(chat.complete("s", "u", ChatOptions()))
    assert len(seen) == 4


@pytest.mark.parametrize("reason", ["max_tokens", "refusal"])
def test_model_finish_reason(reason):
    chat, _ = _chat("m", stop_reason=reason)
    result = asyncio.run(chat.complete("s", "u", ChatOptions()))
    assert result.finish_reason == reason
