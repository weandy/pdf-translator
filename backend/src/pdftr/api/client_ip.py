"""只从原始 TCP 对端及显式可信的代理链恢复客户端 IP。"""

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Literal

from fastapi import Request

from pdftr.config import Settings

MAX_FORWARDED_BYTES = 2048
MAX_FORWARDED_HOPS = 16


@dataclass(frozen=True)
class ClientIP:
    address: str | None
    source: Literal["trusted_proxy", "direct", "unknown"]


def _address(raw: str) -> IPv4Address | IPv6Address:
    # 不接受端口、zone ID、引号或 RFC7239 标记，防止不同代理对相同头产生分歧。
    if "%" in raw:
        raise ValueError("IP 不接受 zone ID")
    address = ip_address(raw)
    return address.ipv4_mapped if isinstance(address, IPv6Address) and address.ipv4_mapped else address


def resolve_client_ip(request: Request, settings: Settings) -> ClientIP:
    """Uvicorn 必须以 --no-proxy-headers 运行，request.client 才是原始 peer。"""
    unknown = ClientIP(None, "unknown")
    peer = request.scope.get("client")
    if not peer:
        return unknown
    try:
        current = _address(peer[0])
        networks = tuple(ip_network(cidr) for cidr in settings.trusted_proxy_cidrs)
    except (ValueError, TypeError):
        return unknown

    def trusted(address: IPv4Address | IPv6Address) -> bool:
        return any(address in network for network in networks)

    forwarded = request.headers.getlist("x-forwarded-for")
    if not trusted(current):
        # 未配置可信链时出现转发头也不能把代理容器地址记成提交者。
        if not networks and forwarded:
            return unknown
        return ClientIP(str(current), "direct")
    if len(forwarded) != 1 or len(forwarded[0]) > MAX_FORWARDED_BYTES:
        return unknown
    chain = forwarded[0].split(",")
    if len(chain) > MAX_FORWARDED_HOPS:
        return unknown
    try:
        addresses = [_address(value.strip()) for value in chain]
    except ValueError:
        return unknown
    for candidate in reversed(addresses):
        if not trusted(current):
            break
        current = candidate
    if trusted(current):
        return unknown
    return ClientIP(str(current), "trusted_proxy")
