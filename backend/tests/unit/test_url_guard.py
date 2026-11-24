"""出站 LLM、解析服务与代理 URL 的 SSRF 防护与边界测试。"""

import asyncio
import socket
from typing import Any

import httpcore2
import pytest

from pdftr.domain.url_guard import (
    PinnedPublicAsyncBackend,
    PinnedPublicSyncBackend,
    build_safe_async_transport,
    build_safe_sync_transport,
    resolve_public_ips,
)
from pdftr.pipeline.url_guard import validate_llm_url, validate_parser_url, validate_proxy_url


def _fake_addrinfo(*ips: str):
    def _resolver(host: str, port: object, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        result: list[tuple[object, ...]] = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
            result.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return result

    return _resolver


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://[::1]:11434/v1",
        "http://[::ffff:127.0.0.1]/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://[::ffff:169.254.169.254]/latest/meta-data",
        "http://[fe80::1]/v1",
        "http://10.0.0.8/v1",
        "http://172.16.0.1/v1",
        "http://192.168.1.10/v1",
        "http://100.64.0.1/v1",
        "http://0.0.0.0/v1",
        "http://224.0.0.1/v1",
        "https://localhost/v1",
        "https://api.localhost/v1",
        "https://svc.local/v1",
        "https://svc.internal/v1",
        "https://metadata.google.internal/computeMetadata/v1",
    ],
)
def test_validate_llm_url_rejects_private_and_metadata_targets(url: str) -> None:
    with pytest.raises(ValueError):
        validate_llm_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "ftp://8.8.8.8/v1",
        "https://user:pass@8.8.8.8/v1",
        "https://user@8.8.8.8/v1",
        "https://8.8.8.8/v1?redirect=1",
        "https://8.8.8.8/v1?",
        "https://8.8.8.8/v1#frag",
        "https://8.8.8.8/v1#",
        "https://8.8.8.8:99999/v1",
        "https://8.8.8.8/v1\nheader: bad",
    ],
)
def test_validate_llm_url_rejects_malformed_credentials_query_and_fragment(url: str) -> None:
    with pytest.raises(ValueError):
        validate_llm_url(url)
    with pytest.raises(ValueError):
        validate_llm_url(url, allow_private=True)


def test_validate_llm_url_public_https_and_dns_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate_llm_url("https://8.8.8.8/v1/") == "https://8.8.8.8/v1"
    assert validate_llm_url("http://8.8.8.8/v1") == "http://8.8.8.8/v1"
    with pytest.raises(ValueError, match="https"):
        validate_llm_url("http://8.8.8.8/v1", require_https=True)
    assert validate_llm_url("https://8.8.8.8/v1", require_https=True) == "https://8.8.8.8/v1"
    assert validate_llm_url("http://localhost:11434/v1/", allow_private=True) == "http://localhost:11434/v1"

    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("93.184.216.34"))
    assert validate_llm_url("https://api.example.com/v1/") == "https://api.example.com/v1"

    # DNS 混入内网 IP 时必须拒绝
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("93.184.216.34", "10.0.0.5"))
    with pytest.raises(ValueError, match="内网或本机"):
        validate_llm_url("https://dual.example.com/v1")

    def _dns_fail(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(socket, "getaddrinfo", _dns_fail)
    with pytest.raises(ValueError, match="无法解析"):
        validate_llm_url("https://nonexistent.example.com/v1")


def test_validate_parser_url_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    # 公网模式强制 HTTPS 且禁内网
    with pytest.raises(ValueError, match="https"):
        validate_parser_url("http://8.8.8.8")
    with pytest.raises(ValueError):
        validate_parser_url("https://127.0.0.1:8080")
    assert validate_parser_url("https://8.8.8.8/") == "https://8.8.8.8"

    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("93.184.216.34"))
    assert validate_parser_url("https://mineru.net/") == "https://mineru.net"

    # 私有模式允许 HTTP 和内网地址，但仍拒绝账密/查询参数
    assert validate_parser_url("http://127.0.0.1:8080/", allow_private=True) == "http://127.0.0.1:8080"
    assert validate_parser_url("http://192.168.1.20:8000", allow_private=True) == "http://192.168.1.20:8000"
    with pytest.raises(ValueError):
        validate_parser_url("http://user:pass@127.0.0.1:8080", allow_private=True)


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://8.8.8.8:8080",
        "https://8.8.8.8:8443",
        "socks5://8.8.8.8:1080",
        "socks5h://user:p%40ss@8.8.8.8:1080",
        "http://alice:secret@10.0.0.2:3128",
    ],
)
def test_validate_proxy_url_accepts_supported_schemes_and_credentials(proxy_url: str) -> None:
    assert validate_proxy_url(proxy_url) == proxy_url


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://169.254.169.254:80",
        "socks5://169.254.1.2:1080",
        "http://[fe80::1]:8080",
        "http://[::ffff:169.254.169.254]:80",
        "http://metadata.google.internal:80",
        "socks5h://user:pass@metadata.google.internal:1080",
    ],
)
def test_validate_proxy_url_always_rejects_link_local_and_metadata(proxy_url: str) -> None:
    with pytest.raises(ValueError):
        validate_proxy_url(proxy_url, allow_private=False)
    with pytest.raises(ValueError):
        validate_proxy_url(proxy_url, allow_private=True)


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://127.0.0.1:7890",
        "socks5://[::1]:1080",
        "http://[::ffff:127.0.0.1]:7890",
        "http://localhost:7890",
        "http://sub.localhost:7890",
        "http://0.0.0.0:7890",
    ],
)
def test_validate_proxy_url_loopback_requires_allow_private(proxy_url: str) -> None:
    with pytest.raises(ValueError):
        validate_proxy_url(proxy_url, allow_private=False)


def test_validate_proxy_url_allows_loopback_when_private_enabled_and_rejects_bad_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert validate_proxy_url("http://127.0.0.1:7890/", allow_private=True) == "http://127.0.0.1:7890"
    assert validate_proxy_url("socks5://[::1]:1080", allow_private=True) == "socks5://[::1]:1080"

    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("127.0.0.1"))
    assert validate_proxy_url("socks5h://user:pass@localhost:1080", allow_private=True) == (
        "socks5h://user:pass@localhost:1080"
    )
    with pytest.raises(ValueError, match="回环"):
        validate_proxy_url("http://loopback-alias.example.com:7890", allow_private=False)

    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("169.254.169.254"))
    with pytest.raises(ValueError, match="链路本地"):
        validate_proxy_url("http://meta-alias.example.com:80", allow_private=True)

    for invalid in (
        "",
        "ftp://8.8.8.8:21",
        "socks4://8.8.8.8:1080",
        "http://8.8.8.8:8080/path",
        "http://8.8.8.8:8080?x=1",
        "http://8.8.8.8:8080#hash",
        "http://8.8.8.8:99999",
    ):
        with pytest.raises(ValueError):
            validate_proxy_url(invalid, allow_private=True)


@pytest.mark.parametrize(
    "bad_proxy",
    [
        "socks5h://user:SECRET_PROXY_PASS_999@[invalid-ipv6:1080",
        "http://user:SECRET_PROXY_PASS_999@8.8.8.8:badport",
        "http://user:SECRET_PROXY_PASS_999@8.8.8.8:99999",
        "http://user:SECRET_PROXY_PASS_999@169.254.169.254:80",
    ],
)
def test_validate_proxy_url_never_echoes_credentials_in_errors(bad_proxy: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_proxy_url(bad_proxy, allow_private=False)
    assert "SECRET_PROXY_PASS_999" not in str(exc_info.value)


def test_pinned_public_backends_connect_to_resolved_ip_and_block_private(monkeypatch: pytest.MonkeyPatch) -> None:
    async_hosts: list[str] = []
    sync_hosts: list[str] = []

    class _DummyAsyncBackend(httpcore2.AsyncNetworkBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore2.AsyncNetworkStream:
            del port, timeout, local_address, socket_options
            async_hosts.append(host)
            return httpcore2.AsyncNetworkStream()

        async def sleep(self, seconds: float) -> None:
            del seconds

    class _DummySyncBackend(httpcore2.NetworkBackend):
        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore2.NetworkStream:
            del port, timeout, local_address, socket_options
            sync_hosts.append(host)
            return httpcore2.NetworkStream()

    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("93.184.216.34"))
    assert resolve_public_ips("api.example.com", 443) == ["93.184.216.34"]

    async_backend = PinnedPublicAsyncBackend(_DummyAsyncBackend())
    sync_backend = PinnedPublicSyncBackend(_DummySyncBackend())
    asyncio.run(async_backend.connect_tcp("api.example.com", 443))
    sync_backend.connect_tcp("api.example.com", 443)
    assert async_hosts == ["93.184.216.34"]
    assert sync_hosts == ["93.184.216.34"]

    # DNS 重绑定返回私网/回环 IP 时：底层 connect_tcp 绝不被调用且立即抛错
    for private_ip in ("127.0.0.1", "10.0.0.1"):
        monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo(private_ip))
        async_hosts.clear()
        sync_hosts.clear()
        with pytest.raises(httpcore2.ConnectError, match="内网或本机"):
            asyncio.run(async_backend.connect_tcp("rebind.example.com", 443))
        with pytest.raises(httpcore2.ConnectError, match="内网或本机"):
            sync_backend.connect_tcp("rebind.example.com", 443)
        assert async_hosts == []
        assert sync_hosts == []

    # 工厂函数在公网直连模式下自动挂载固定后端
    async_tr = build_safe_async_transport(allow_private=False)
    sync_tr = build_safe_sync_transport(allow_private=False)
    assert isinstance(async_tr._pool, httpcore2.AsyncConnectionPool)
    assert isinstance(async_tr._pool._network_backend, PinnedPublicAsyncBackend)
    assert isinstance(sync_tr._pool, httpcore2.ConnectionPool)
    assert isinstance(sync_tr._pool._network_backend, PinnedPublicSyncBackend)
