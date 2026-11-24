"""出站 URL 与代理安全校验及公网 IP 套接字固定（domain 层，不依赖其他 pdftr 模块）。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore2
import httpx2

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "metadata.goog", "instance-data"})
_LOOPBACK_HOSTS = frozenset({"localhost", "localhost.localdomain"})
_PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".internal")
_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_MASKED_BAD_PROXY = "***（代理格式异常）"


def _has_whitespace_or_control(value: str) -> bool:
    return any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _unwrap_ip(address: IPAddress) -> IPAddress:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_link_local(address: IPAddress) -> bool:
    ip = _unwrap_ip(address)
    return ip.is_link_local


def _is_loopback_or_unspecified(address: IPAddress) -> bool:
    ip = _unwrap_ip(address)
    return ip.is_loopback or ip.is_unspecified


def _is_non_public_ip(address: IPAddress) -> bool:
    ip = _unwrap_ip(address)
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    return isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NETWORK


def _parse_ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _resolve_addresses(host: str, port: int | None, *, label: str) -> set[IPAddress]:
    literal = _parse_ip_literal(host)
    if literal is not None:
        return {literal}
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"{label}无法解析") from exc
    addresses: set[IPAddress] = set()
    for item in infos:
        raw_ip = item[4][0]
        if isinstance(raw_ip, str):
            # 去掉 IPv6 作用域后缀（如 fe80::1%eth0）以便统一判定地址类型
            addresses.add(ipaddress.ip_address(raw_ip.split("%", 1)[0]))
    if not addresses:
        raise ValueError(f"{label}无法解析")
    return addresses


def _safe_urlsplit(
    cleaned: str, *, format_error: str
) -> tuple[str, str, int | None, str, str, str, str | None, str | None]:
    """安全解析 URL，严禁将底层 ValueError 中的原始 URL、authority 或代理账密回显给调用方。"""
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
        hostname = parsed.hostname or ""
    except ValueError:
        raise ValueError(format_error) from None
    return (
        parsed.scheme,
        hostname,
        port,
        parsed.path,
        parsed.query,
        parsed.fragment,
        parsed.username,
        parsed.password,
    )


def _validate_http_endpoint_url(
    value: str,
    *,
    label: str,
    allow_private: bool,
    require_https: bool,
    allow_query: bool = False,
    resolve_dns: bool = True,
) -> str:
    cleaned = value.strip()
    format_error = f"{label}格式无效，请检查协议、主机与端口"
    if not cleaned or _has_whitespace_or_control(cleaned):
        raise ValueError(format_error)
    scheme, hostname, port, _, query, fragment, username, password = _safe_urlsplit(cleaned, format_error=format_error)
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{label}端口无效")

    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if scheme not in allowed_schemes:
        scheme_hint = "https" if require_https else "http/https"
        raise ValueError(f"{label}必须是 {scheme_hint} URL")
    if not hostname or username is not None or password is not None:
        raise ValueError(f"{label}不能包含账号密码且必须提供有效主机名")
    if not allow_query and (query or fragment or "?" in cleaned or "#" in cleaned):
        raise ValueError(f"{label}不能包含查询参数或片段")
    if allow_private:
        return cleaned.rstrip("/")

    host = hostname.rstrip(".").lower()
    if host in _LOOPBACK_HOSTS or host in _METADATA_HOSTS or host.endswith(_PRIVATE_HOST_SUFFIXES):
        raise ValueError(f"{label}不能访问内网域名")
    literal = _parse_ip_literal(host)
    if literal is not None:
        if _is_non_public_ip(literal):
            raise ValueError(f"{label}不能访问内网或本机地址")
        return cleaned.rstrip("/")
    if not resolve_dns:
        return cleaned.rstrip("/")
    default_port = 443 if scheme == "https" else 80
    addresses = _resolve_addresses(host, port or default_port, label=label)
    if any(_is_non_public_ip(address) for address in addresses):
        raise ValueError(f"{label}不能访问内网或本机地址")
    return cleaned.rstrip("/")


def validate_llm_url(value: str, *, allow_private: bool = False, require_https: bool = False) -> str:
    """校验 LLM 接口地址，禁止账密、查询参数和片段；公网模式拦截内网及元数据目标。"""
    return _validate_http_endpoint_url(
        value,
        label="翻译服务地址",
        allow_private=allow_private,
        require_https=require_https,
    )


def validate_parser_url(value: str, *, allow_private: bool = False) -> str:
    """校验文档解析服务地址：公网模式强制 HTTPS 并禁止内网，私有模式允许 HTTP 与内网地址。"""
    return _validate_http_endpoint_url(
        value,
        label="解析服务地址",
        allow_private=allow_private,
        require_https=not allow_private,
    )


def validate_outbound_request_url(
    value: str,
    *,
    allow_private: bool = False,
    require_https: bool = False,
    label: str = "目标地址",
    resolve_dns: bool = True,
) -> str:
    """在出站请求发出前校验实际目标 URL（允许查询参数，用于预签名地址或防 DNS 重绑定/重定向）。"""
    return _validate_http_endpoint_url(
        value,
        label=label,
        allow_private=allow_private,
        require_https=require_https,
        allow_query=True,
        resolve_dns=resolve_dns,
    )


def validate_proxy_url(value: str, *, allow_private: bool = False) -> str:
    """校验出站代理地址：仅允许 http/https/socks5/socks5h，允许含账密；始终禁止链路本地与元数据主机。"""
    cleaned = value.strip()
    format_error = "代理地址格式无效，请检查协议、主机与端口"
    if not cleaned or _has_whitespace_or_control(cleaned):
        raise ValueError(format_error)
    scheme, hostname, port, path, query, fragment, _, _ = _safe_urlsplit(cleaned, format_error=format_error)
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("代理地址端口无效")
    if scheme not in _PROXY_SCHEMES:
        raise ValueError("代理地址仅支持 http、https、socks5 或 socks5h 协议")
    if not hostname:
        raise ValueError("代理地址缺少主机名")
    if path not in {"", "/"}:
        raise ValueError("代理地址不能包含路径")
    if query or fragment or "?" in cleaned or "#" in cleaned:
        raise ValueError("代理地址不能包含查询参数或片段")

    host = hostname.rstrip(".").lower()
    if host in _METADATA_HOSTS:
        raise ValueError("代理地址不能访问云元数据主机")
    if not allow_private and (host in _LOOPBACK_HOSTS or host.endswith(".localhost")):
        raise ValueError("代理地址不能访问本机回环地址")

    default_port = 443 if scheme == "https" else (80 if scheme == "http" else 1080)
    addresses = _resolve_addresses(host, port or default_port, label="代理地址")
    if any(_is_link_local(address) for address in addresses):
        raise ValueError("代理地址不能访问链路本地或云元数据地址")
    if not allow_private and any(_is_loopback_or_unspecified(address) for address in addresses):
        raise ValueError("代理地址不能访问本机回环地址")
    return cleaned.rstrip("/")


def safe_mask_proxy_url(url: str) -> str:
    """安全掩码代理 URL：保留 scheme 与 host:port、隐藏账密；格式异常绝不回显原始字符串或抛异常。"""
    cleaned = url.strip()
    if not cleaned:
        return ""
    if _has_whitespace_or_control(cleaned):
        return _MASKED_BAD_PROXY
    try:
        scheme, hostname, port, _, _, _, username, password = _safe_urlsplit(cleaned, format_error=_MASKED_BAD_PROXY)
    except ValueError:
        return _MASKED_BAD_PROXY
    if not scheme or not hostname:
        return _MASKED_BAD_PROXY
    if port is not None and not 1 <= port <= 65535:
        return _MASKED_BAD_PROXY
    host = f"[{hostname}]" if ":" in hostname else hostname
    host_port = f"{host}:{port}" if port is not None else host
    if username is not None or password is not None:
        return f"{scheme}://***@{host_port}"
    return f"{scheme}://{host_port}"


def resolve_public_ips(host: str, port: int = 443, *, label: str = "目标地址") -> list[str]:
    """校验主机非内网/回环/元数据并执行单次 DNS 解析，确保全部解析地址均为公网 IP 并返回去重列表。"""
    cleaned = host.strip()
    if not cleaned or _has_whitespace_or_control(cleaned):
        raise ValueError(f"{label}主机名无效")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    norm_host = cleaned.rstrip(".").lower()
    if (
        not norm_host
        or norm_host in _LOOPBACK_HOSTS
        or norm_host in _METADATA_HOSTS
        or norm_host.endswith(_PRIVATE_HOST_SUFFIXES)
    ):
        raise ValueError(f"{label}不能访问内网域名")
    literal = _parse_ip_literal(norm_host)
    if literal is not None:
        if _is_non_public_ip(literal):
            raise ValueError(f"{label}不能访问内网或本机地址")
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(norm_host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"{label}无法解析") from exc
    ips: list[str] = []
    seen: set[str] = set()
    for item in infos:
        raw_ip = item[4][0]
        if not isinstance(raw_ip, str):
            continue
        address = ipaddress.ip_address(raw_ip.split("%", 1)[0])
        if _is_non_public_ip(address):
            raise ValueError(f"{label}不能访问内网或本机地址")
        normalized_ip = str(address)
        if normalized_ip not in seen:
            seen.add(normalized_ip)
            ips.append(normalized_ip)
    if not ips:
        raise ValueError(f"{label}无法解析")
    return ips


class PinnedPublicAsyncBackend(httpcore2.AsyncNetworkBackend):
    """套接字级公网 IP 固定异步后端：建连前单次解析并直接连接已校验公网 IP，保留原始域名 SNI 与证书校验。"""

    def __init__(self, inner: httpcore2.AsyncNetworkBackend | None = None) -> None:
        if inner is None:
            from httpcore2._backends.auto import AutoBackend

            inner = AutoBackend()
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        try:
            ips = await asyncio.to_thread(resolve_public_ips, host, port)
        except ValueError as exc:
            raise httpcore2.ConnectError(str(exc)) from exc
        last_exc: Exception | None = None
        for ip in ips:
            try:
                return await self._inner.connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedPublicSyncBackend(httpcore2.NetworkBackend):
    """套接字级公网 IP 固定同步后端：建连前单次解析并直接连接已校验公网 IP，防 DNS Rebinding。"""

    def __init__(self, inner: httpcore2.NetworkBackend | None = None) -> None:
        self._inner = httpcore2.SyncBackend() if inner is None else inner

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.NetworkStream:
        try:
            ips = resolve_public_ips(host, port)
        except ValueError as exc:
            raise httpcore2.ConnectError(str(exc)) from exc
        last_exc: Exception | None = None
        for ip in ips:
            try:
                return self._inner.connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.NetworkStream:
        return self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def build_safe_async_transport(
    *,
    proxy: str | None = None,
    allow_private: bool = False,
) -> httpx2.AsyncHTTPTransport:
    """构造带公网 IP 固定能力的异步传输层；启用代理或私有模式时返回标准传输层。"""
    cleaned_proxy = proxy.strip() if proxy else None
    if allow_private or cleaned_proxy is not None:
        return httpx2.AsyncHTTPTransport(proxy=cleaned_proxy)
    transport = httpx2.AsyncHTTPTransport()
    if isinstance(transport._pool, httpcore2.AsyncConnectionPool):
        transport._pool._network_backend = PinnedPublicAsyncBackend(transport._pool._network_backend)
    return transport


def build_safe_sync_transport(
    *,
    proxy: str | None = None,
    allow_private: bool = False,
) -> httpx2.HTTPTransport:
    """构造带公网 IP 固定能力的同步传输层；启用代理或私有模式时返回标准传输层。"""
    cleaned_proxy = proxy.strip() if proxy else None
    if allow_private or cleaned_proxy is not None:
        return httpx2.HTTPTransport(proxy=cleaned_proxy)
    transport = httpx2.HTTPTransport()
    if isinstance(transport._pool, httpcore2.ConnectionPool):
        transport._pool._network_backend = PinnedPublicSyncBackend(transport._pool._network_backend)
    return transport
