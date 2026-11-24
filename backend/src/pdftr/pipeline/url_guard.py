"""出站 URL 安全校验与 httpx2 请求前重检钩子。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx2

from pdftr.domain.url_guard import (
    validate_llm_url,
    validate_outbound_request_url,
    validate_parser_url,
    validate_proxy_url,
)

__all__ = [
    "make_outbound_request_hook",
    "validate_llm_url",
    "validate_outbound_request_url",
    "validate_parser_url",
    "validate_proxy_url",
]


def make_outbound_request_hook(
    *,
    allow_private: bool = False,
    require_https: bool = False,
    label: str = "出站请求地址",
    resolve_dns: bool = True,
    error_factory: Callable[[str], Exception] | None = None,
) -> Callable[[httpx2.Request], Awaitable[None]]:
    """构造 httpx2 async request event hook：发请求前重检目标，阻断 DNS 重绑定与跨源跳转私网。"""

    async def _guard(request: httpx2.Request) -> None:
        if allow_private:
            return
        try:
            validate_outbound_request_url(
                str(request.url),
                allow_private=False,
                require_https=require_https,
                label=label,
                resolve_dns=resolve_dns,
            )
        except ValueError as exc:
            if error_factory is not None:
                raise error_factory(str(exc)) from None
            raise httpx2.ConnectError(str(exc), request=request) from None

    return _guard
