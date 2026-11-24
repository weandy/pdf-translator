"""验证两个适配器在异步请求交错时的降级记忆与结果标志。"""

import asyncio
import json
from collections import defaultdict

import httpx2
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from pdftr.domain.llm import ChatOptions
from pdftr.translation.anthropic_compat import AnthropicChat
from pdftr.translation.openai_compat import OpenAICompatChat


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_concurrent_400_merges_memory_without_marking_inflight_success(protocol):
    async def run():
        users = ("first", "second", "inflight")
        arrived = {user: asyncio.Event() for user in users}
        respond = {user: asyncio.Event() for user in users}
        seen: dict[str, list[dict]] = defaultdict(list)
        first, second = ("reasoning_effort", "temperature") if protocol == "openai" else ("output_config", "thinking")
        if protocol == "openai":
            ok = {
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": "synthetic",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "译文"}}],
            }
        else:
            ok = {
                "id": "synthetic",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "译文"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }

        async def handler(request):
            body = json.loads(request.content)
            user = body["messages"][-1]["content"]
            seen[user].append(body)
            if user in arrived and len(seen[user]) == 1:
                arrived[user].set()
                await respond[user].wait()
                if user != "inflight":
                    field = first if user == "first" else second
                    return httpx2.Response(400, json={"type": "error", "error": {"param": field}})
            return httpx2.Response(200, json=ok)

        url = "https://example.invalid"
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            chat = (
                OpenAICompatChat(
                    base_url=url, api_key="k", model="synthetic", dialect="deepseek",
                    client=AsyncOpenAI(base_url=url, api_key="k", http_client=client),
                )
                if protocol == "openai"
                else AnthropicChat(
                    base_url=url, api_key="k", model="claude-sonnet-4-6",
                    client=AsyncAnthropic(base_url=url, api_key="k", http_client=client),
                )
            )  # fmt: skip
            careful = ChatOptions(thinking="low", transport_retries=0)
            second_opts = ChatOptions(transport_retries=0) if protocol == "openai" else careful
            async with asyncio.timeout(5), asyncio.TaskGroup() as group:
                tasks = {
                    user: group.create_task(chat.complete("s", user, second_opts if user == "second" else careful))
                    for user in users
                }
                await asyncio.gather(*(event.wait() for event in arrived.values()))
                for user in users:
                    respond[user].set()
                    await tasks[user]

            assert [tasks[user].result().compatibility_fallback for user in users] == [True, True, False]
            assert [len(seen[user]) for user in users] == [2, 2, 1]
            assert first in seen["first"][0] and first not in seen["first"][1]
            assert second in seen["second"][0] and second not in seen["second"][1]
            if protocol == "anthropic":
                assert first in seen["second"][0] and first not in seen["second"][1]

            for user, opts in (("future-first", careful), ("future-second", second_opts)):
                result = await chat.complete("s", user, opts)
                assert result.compatibility_fallback
                (body,) = seen[user]
                assert not {first, second} & body.keys()

    asyncio.run(run())
