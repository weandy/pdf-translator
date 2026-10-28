"""接口层的依赖：容器、来自请求头的密钥。"""

from typing import Literal

from fastapi import Header, Request

from pdftr.container import Container
from pdftr.pipeline.runner import Pipeline
from pdftr.pipeline.secrets import Secrets


def container(request: Request) -> Container:
    return request.app.state.container


def pipeline(request: Request) -> Pipeline:
    return container(request).pipeline


def secrets(
    request: Request,
    x_llm_base_url: str | None = Header(default=None),
    x_llm_key: str | None = Header(default=None),
    x_llm_model: str | None = Header(default=None),
    x_parser_key: str | None = Header(default=None),
    x_paddle_key: str | None = Header(default=None),
    x_llm_protocol: Literal["openai", "anthropic"] | None = Header(default=None),
) -> Secrets:
    """用户在浏览器里保存的 Key 随请求头发来；没有时用部署者配置的共享 Key。"""
    return container(request).secrets(
        base_url=x_llm_base_url, api_key=x_llm_key, model=x_llm_model, parser_token=x_parser_key,
        paddle_token=x_paddle_key, protocol=x_llm_protocol,
    )  # fmt: skip
