"""接口层的依赖：容器、来自请求头的密钥。"""

import ipaddress
import socket
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from pdftr.container import Container
from pdftr.pipeline.runner import Pipeline
from pdftr.pipeline.secrets import Secrets


def container(request: Request) -> Container:
    return request.app.state.container


def pipeline(request: Request) -> Pipeline:
    return container(request).pipeline


def validate_user_llm_url(value: str, *, allow_private: bool = False) -> str:
    """限制浏览器传入的 LLM 地址，避免公开部署被用作内网请求代理。"""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="翻译服务地址必须是 http/https URL，且不能包含账号密码")
    if parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="翻译服务地址不能包含查询参数或片段")
    if allow_private:
        return value.rstrip("/")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal"} or host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise HTTPException(status_code=400, detail="翻译服务地址不能访问内网域名")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise HTTPException(status_code=400, detail="翻译服务地址无法解析") from exc
    if any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        for address in addresses
    ):
        raise HTTPException(status_code=400, detail="翻译服务地址不能访问内网或本机地址")
    return value.rstrip("/")


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
    if x_llm_base_url is not None:
        x_llm_base_url = validate_user_llm_url(
            x_llm_base_url, allow_private=request.app.state.container.settings.allow_private_llm_urls
        )
    return container(request).secrets(
        base_url=x_llm_base_url, api_key=x_llm_key, model=x_llm_model, parser_token=x_parser_key,
        paddle_token=x_paddle_key, protocol=x_llm_protocol,
    )  # fmt: skip
