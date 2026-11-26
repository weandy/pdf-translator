"""OpenAI 兼容适配器：检查发给各家的请求体（思考开关方言）与用量解析，不访问网络。"""

import asyncio
import json

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from pdftr.domain.llm import ChatError, ChatOptions, ChatResult
from pdftr.translation.openai_compat import FallbackChat, OpenAICompatChat, guess_dialect

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
        ("deepseek", "xhigh", {"thinking": {"type": "enabled"}, "reasoning_effort": "xhigh"}, ["temperature"]),
        ("openai", "off", {"reasoning_effort": "none"}, ["temperature", "thinking"]),
        ("openai", "xhigh", {"reasoning_effort": "high"}, ["temperature", "thinking"]),
        ("generic", "low", {"temperature": 0.3}, ["thinking", "reasoning_effort"]),
        ("generic", "xhigh", {"temperature": 0.3}, ["thinking", "reasoning_effort"]),
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


@pytest.mark.parametrize(
    ("base_url", "model", "expected"),
    [
        ("https://integrate.api.nvidia.com/v1", "nvidia/nemotron-3-ultra-550b", "nemotron"),
        ("https://integrate.api.nvidia.com/v1", "nvidia/nemotron-ultra-253b-v1", "nemotron"),
        ("https://integrate.api.nvidia.com/v1", "nvidia/nemotron-3-super-120b", "nemotron_off"),
        ("https://integrate.api.nvidia.com/v1", "nvidia/nemotron-super-vl-8b", "nemotron_off"),
        ("https://integrate.api.nvidia.com/v1", "meta/llama-3.3-70b-instruct", "generic"),
        ("https://proxy.example.com/v1", "nvidia/nemotron-3-ultra-550b", "nemotron"),
        ("https://proxy.example.com/v1", "nvidia/nemotron-3-super-120b", "nemotron_off"),
        ("https://proxy.example.com/v1", "nvidia/nemotron-mini-4b", "generic"),
    ],
)
def test_guess_dialect_nim_and_nemotron_models(base_url, model, expected):
    assert guess_dialect(base_url, model) == expected


@pytest.mark.parametrize(
    ("dialect", "thinking", "expect", "absent"),
    [
        (
            "nemotron",
            "off",
            {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0.3},
            ["reasoning_effort"],
        ),
        ("nemotron", "low", {"reasoning_effort": "low"}, ["chat_template_kwargs", "temperature"]),
        ("nemotron", "xhigh", {"reasoning_effort": "high"}, ["chat_template_kwargs", "temperature"]),
        (
            "nemotron_off",
            "off",
            {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0.3},
            ["reasoning_effort"],
        ),
        (
            "nemotron_off",
            "low",
            {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0.3},
            ["reasoning_effort"],
        ),
        (
            "nemotron_off",
            "xhigh",
            {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0.3},
            ["reasoning_effort"],
        ),
    ],
)
def test_nemotron_request_body(dialect, thinking, expect, absent):
    chat, seen = _chat(dialect)
    asyncio.run(chat.complete("sys", "1|text", ChatOptions(thinking=thinking)))
    body = seen[0]
    for k, v in expect.items():
        assert body[k] == v
    for k in absent:
        assert k not in body


def test_nemotron_off_preserves_extra_body_on_400_and_disabled_flags():
    # 即使 _disabled 含有 drop_thinking，nemotron_off 仍必须保留 chat_template_kwargs
    chat, seen = _chat("nemotron_off", replies=((400, {"param": "temperature"}),))
    chat._disabled.add("drop_thinking")
    result = asyncio.run(chat.complete("s", "u", ChatOptions(thinking="low", transport_retries=0)))
    assert result.compatibility_fallback is True
    assert len(seen) == 2
    assert seen[1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "temperature" not in seen[1]

    # 400 提示 thinking/chat_template_kwargs 不支持时，绝不拆掉 extra_body
    chat2, seen2 = _chat("nemotron_off", status=400, error={"message": "unknown extra_body.thinking"})
    with pytest.raises(ChatError) as caught:
        asyncio.run(chat2.complete("s", "u", ChatOptions(transport_retries=0)))
    assert caught.value.status_code == 400
    assert len(seen2) == 1
    assert chat2.request_kwargs(ChatOptions())["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("dialect", ["nemotron", "nemotron_off"])
@pytest.mark.parametrize(
    ("error", "retryable", "expected_code"),
    [
        ({"message": "Too Many Requests"}, True, ""),
        ({"message": "rate limit", "code": "rate_limit_exceeded"}, True, "rate_limit_exceeded"),
        ({"message": "quota", "code": "insufficient_quota"}, False, "insufficient_quota"),
        ({"message": "billing", "code": "billing_hard_limit_reached"}, False, "billing_hard_limit_reached"),
        ({"message": "disabled", "type": "account_deactivated"}, False, ""),
    ],
)
def test_nim_429_retryable_and_billing_classification(dialect, error, retryable, expected_code):
    chat, seen = _chat(dialect, status=429, error=error)
    with pytest.raises(ChatError) as caught:
        asyncio.run(chat.complete("s", "u", ChatOptions(transport_retries=0)))
    assert len(seen) == 1
    assert caught.value.retryable is retryable
    assert caught.value.status_code == 429
    assert caught.value.error_code == expected_code


def test_disable_redirects_builds_non_redirecting_http_client_and_closes_cleanly():
    chat = OpenAICompatChat(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="k",
        model="nvidia/nemotron-3-super-120b",
        disable_redirects=True,
        default_max_retries=1,
    )
    assert chat.dialect == "nemotron_off"
    assert chat._http_client is not None
    assert chat._http_client.follow_redirects is False
    import httpcore2

    from pdftr.domain.url_guard import PinnedPublicAsyncBackend

    assert isinstance(chat._http_client._transport, httpx2.AsyncHTTPTransport)
    assert isinstance(chat._http_client._transport._pool, httpcore2.AsyncConnectionPool)
    assert isinstance(chat._http_client._transport._pool._network_backend, PinnedPublicAsyncBackend)
    asyncio.run(chat.aclose())
    assert chat._http_client.is_closed is True


def test_default_openai_chat_blocks_302_redirect_and_respects_zero_retry_cap():
    seen: list[tuple[str, str]] = []

    def redirect_handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers.get("authorization", "")))
        return httpx2.Response(302, headers={"location": "https://attacker.example/steal"})

    chat = OpenAICompatChat(
        base_url="https://api.example/v1",
        api_key="sk-secret-key",
        model="m",
        transport=httpx2.MockTransport(redirect_handler),
    )
    try:
        assert chat._http_client is not None and chat._http_client.follow_redirects is False
        with pytest.raises(ChatError) as exc:
            asyncio.run(chat.complete("s", "u", ChatOptions(transport_retries=0)))
        assert exc.value.status_code == 302
        # 绝不跟随 302 到攻击者域名
        assert len(seen) == 1
        assert seen[0][0].startswith("https://api.example/v1")
    finally:
        asyncio.run(chat.aclose())

    # default_max_retries=0 时，即使调用传入默认 transport_retries=3（如术语抽取）也绝不重试
    retry_calls = 0

    def fail_503(_request: httpx2.Request) -> httpx2.Response:
        nonlocal retry_calls
        retry_calls += 1
        return httpx2.Response(503, json={"error": {"message": "busy"}})

    free_chat = OpenAICompatChat(
        base_url="https://api.example/v1",
        api_key="sk-free",
        model="m",
        default_max_retries=0,
        transport=httpx2.MockTransport(fail_503),
    )
    try:
        with pytest.raises(ChatError) as exc2:
            asyncio.run(free_chat.complete("s", "u", ChatOptions()))
        assert exc2.value.status_code == 503
        assert retry_calls == 1
    finally:
        asyncio.run(free_chat.aclose())


class _FakeMember:
    def __init__(
        self,
        model: str,
        *,
        provider_id: str = "",
        error: ChatError | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.model = model
        self.provider_id = provider_id or f"openai:nemotron:{model}"
        self.error = error
        self.close_error = close_error
        self.closed = False
        self.calls = 0

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        del system, user, opts
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ChatResult(text=f"1|来自:{self.model}")

    async def list_models(self) -> list[str]:
        return [self.model]

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def test_fallback_chat_switches_on_429_5xx_or_retryable_and_keeps_primary_identity():
    primary = _FakeMember(
        "nvidia/nemotron-3-ultra-550b",
        provider_id="openai:nemotron:https://integrate.api.nvidia.com/v1",
        error=ChatError("限流", retryable=False, status_code=429),
    )
    fb1 = _FakeMember("fb-1", error=ChatError("服务错误", retryable=True, status_code=503))
    fb2 = _FakeMember("nvidia/nemotron-3-super-120b")
    wrapper = FallbackChat(primary, [fb1, fb2])

    result = asyncio.run(wrapper.complete("s", "1|hi", ChatOptions()))
    assert result.text == "1|来自:nvidia/nemotron-3-super-120b"
    assert result.used_fallback is True
    assert result.actual_model == "nvidia/nemotron-3-super-120b"
    assert wrapper.model == "nvidia/nemotron-3-ultra-550b"
    assert wrapper.provider_id == "openai:nemotron:https://integrate.api.nvidia.com/v1"
    assert (primary.calls, fb1.calls, fb2.calls) == (1, 1, 1)


def test_fallback_chat_does_not_switch_on_non_retryable_401():
    primary = _FakeMember("primary-m", error=ChatError("Key 无效", retryable=False, status_code=401))
    fb = _FakeMember("backup-m")
    wrapper = FallbackChat(primary, [fb])

    with pytest.raises(ChatError, match="Key 无效"):
        asyncio.run(wrapper.complete("s", "1|hi", ChatOptions()))
    assert (primary.calls, fb.calls) == (1, 0)


def test_fallback_chat_aclose_releases_primary_and_all_fallbacks():
    primary = _FakeMember("p", close_error=RuntimeError("primary close failed"))
    fb1 = _FakeMember("b1")
    fb2 = _FakeMember("b2")
    wrapper = FallbackChat(primary, [fb1, fb2])

    with pytest.raises(RuntimeError, match="primary close failed"):
        asyncio.run(wrapper.aclose())
    assert primary.closed and fb1.closed and fb2.closed
