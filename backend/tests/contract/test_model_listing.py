"""配置面板模型查询的真实 SDK 契约：限时、分页、错误与去重，不访问网络。"""

import asyncio
from collections.abc import Callable

import httpx2
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from pdftr.domain.llm import ChatError
from pdftr.translation import anthropic_compat, openai_compat


def _chat(protocol: str, handler: Callable):
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    if protocol == "openai":
        client = AsyncOpenAI(
            base_url="https://example.invalid/v1", api_key="synthetic", max_retries=3, http_client=http
        )
        return openai_compat.OpenAICompatChat(
            base_url="https://example.invalid/v1", api_key="synthetic", model="m", client=client
        )
    client = AsyncAnthropic(base_url="https://example.invalid", api_key="synthetic", max_retries=3, http_client=http)
    return anthropic_compat.AnthropicChat(
        base_url="https://example.invalid", api_key="synthetic", model="m", client=client
    )


def _page(protocol: str, ids: list[str], *, more: bool = False):
    data = [{"id": model, "object": "model", "type": "model", "created": 0} for model in ids]
    if protocol == "openai":
        return {"object": "list", "data": data}
    return {"data": data, "has_more": more, "first_id": ids[0] if ids else None, "last_id": ids[-1] if ids else None}


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_models_are_deduplicated_sorted_and_use_short_timeout(protocol):
    def handler(request):
        assert request.url.path == "/v1/models"
        assert request.extensions["timeout"]["read"] == 15
        return httpx2.Response(200, json=_page(protocol, ["z", "a", "z", " a "]))

    assert asyncio.run(_chat(protocol, handler).list_models()) == ["a", "z"]


def test_anthropic_pagination_collects_all_models():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx2.Response(200, json=_page("anthropic", ["z", "a"], more=True))
        assert request.url.params["after_id"] == "a"
        return httpx2.Response(200, json=_page("anthropic", ["a", "b"]))

    assert asyncio.run(_chat("anthropic", handler).list_models()) == ["a", "b", "z"]
    assert len(seen) == 2


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "Key 无效"), (403, "权限"), (404, "手动填写"), (429, "请求受限"), (503, "HTTP 503")],
)
def test_model_list_errors_have_no_hidden_retries(protocol, status, message):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(status, json={"error": {"message": "synthetic", "type": "synthetic"}})

    with pytest.raises(ChatError, match=message):
        asyncio.run(_chat(protocol, handler).list_models())
    assert len(seen) == 1


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("body", [{"data": [{"id": None}]}, {"data": [{"id": ""}]}, {"data": None}, {"other": []}])
def test_malformed_model_list_is_actionable(protocol, body):
    with pytest.raises(ChatError, match="格式不正确"):
        asyncio.run(_chat(protocol, lambda _: httpx2.Response(200, json=body)).list_models())


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_model_list_total_deadline_cancels_slow_request(protocol, monkeypatch):
    module = openai_compat if protocol == "openai" else anthropic_compat
    # 留足 SDK 构造请求的时间；只让已经进入传输层的等待触发超时，避免负载下抢先耗尽预算。
    monkeypatch.setattr(module, "MODEL_LIST_TIMEOUT_S", 1)
    cancelled = []

    async def handler(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    with pytest.raises(ChatError, match="超时"):
        asyncio.run(_chat(protocol, handler).list_models())
    assert cancelled == [True]


def test_anthropic_pagination_shares_one_total_deadline(monkeypatch):
    monkeypatch.setattr(anthropic_compat, "MODEL_LIST_TIMEOUT_S", 1)
    seen = []

    async def handler(request):
        seen.append(request)
        if len(seen) > 1:
            # 第一页立即成功，第二页保持等待；验证总超时覆盖分页，不依赖多个短 sleep 的调度精度。
            await asyncio.Event().wait()
        return httpx2.Response(200, json=_page("anthropic", [str(len(seen))], more=True))

    with pytest.raises(ChatError, match="超时"):
        asyncio.run(_chat("anthropic", handler).list_models())
    assert len(seen) == 2
