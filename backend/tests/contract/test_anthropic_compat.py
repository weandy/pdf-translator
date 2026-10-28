"""Anthropic 协议适配器：检查请求体（思考参数只发给认得的 Claude 新模型）、用量解析、模型列表，不访问网络。"""

import asyncio
import json

import httpx2
import pytest
from anthropic import AsyncAnthropic

from pdftr.domain.llm import ChatError, ChatOptions
from pdftr.translation.anthropic_compat import AnthropicChat


def _chat(model: str, status: int = 200) -> tuple[AnthropicChat, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if status != 200:
            return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "x"}})
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={
                "data": [{"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                          "created_at": "2026-01-01T00:00:00Z"}],
                "has_more": False, "first_id": "claude-opus-5-5", "last_id": "claude-opus-5-5",
            })  # fmt: skip
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "1|译文"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 400, "cache_creation_input_tokens": 50,
                      "output_tokens": 30},
        })  # fmt: skip

    client = AsyncAnthropic(
        base_url="https://api.example", api_key="k", max_retries=0, default_headers={"x-api-key": "k"},
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )  # fmt: skip
    return AnthropicChat(base_url="https://api.example", api_key="k", model=model, client=client), seen


@pytest.mark.parametrize(
    ("model", "thinking", "expect", "absent"),
    [
        ("claude-opus-5-5", "off", {"output_config": {"effort": "low"}}, ["thinking", "temperature"]),
        ("claude-sonnet-4-6", "low", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}, []),
        # 认不出的模型（第三方兼容接口、旧模型）不发送思考参数
        ("deepseek-flash", "low", {}, ["thinking", "output_config", "temperature"]),
        ("claude-haiku-4-5", "low", {}, ["thinking", "output_config"]),
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


def test_list_models_and_errors():
    chat, _ = _chat("claude-opus-5-5")
    assert asyncio.run(chat.list_models()) == ["claude-opus-5-5"]
    with pytest.raises(ChatError, match="Key 无效"):
        asyncio.run(_chat("m", status=401)[0].complete("s", "u", ChatOptions()))
    with pytest.raises(ChatError, match="手动填写模型名"):
        asyncio.run(_chat("m", status=404)[0].list_models())
