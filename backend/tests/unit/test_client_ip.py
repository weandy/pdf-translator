"""所有地址均为文档保留地址，验证可信链边界及伪造头。"""

import pytest
from starlette.requests import Request

from pdftr.api.client_ip import resolve_client_ip
from pdftr.config import Settings


def _resolve(peer: str | None, forwarded: str | list[str] | None, cidrs: tuple[str, ...] = ("127.0.0.1/32",)):
    headers = []
    for value in [forwarded] if isinstance(forwarded, str) else forwarded or []:
        headers.append((b"x-forwarded-for", value.encode()))
    request = Request(
        {
            "type": "http",
            "headers": headers,
            "client": (peer, 1234) if peer else None,
        }
    )
    return resolve_client_ip(request, Settings(_env_file=None, trusted_proxy_cidrs=cidrs))  # type: ignore[call-arg]


def test_direct_and_spoofed_headers():
    value = _resolve("192.0.2.1", "198.51.100.7")
    assert (value.address, value.source) == ("192.0.2.1", "direct")
    assert _resolve("192.0.2.1", None, ()).source == "direct"
    assert _resolve("192.0.2.1", "198.51.100.7", ()).source == "unknown"


def test_right_to_left_stops_at_first_untrusted_hop():
    value = _resolve("127.0.0.1", "198.51.100.99, 192.0.2.5, 127.0.0.2", ("127.0.0.0/30",))
    assert (value.address, value.source) == ("192.0.2.5", "trusted_proxy")
    assert _resolve("127.0.0.1", "::ffff:192.0.2.5").address == "192.0.2.5"
    assert _resolve("127.0.0.1", "2001:db8::5").address == "2001:db8::5"
    assert _resolve("::ffff:192.0.2.1", None).address == "192.0.2.1"


@pytest.mark.parametrize(
    "forwarded",
    [
        None,
        "",
        "127.0.0.1",
        "unknown",
        "192.0.2.1:123",
        "[2001:db8::1]",
        "fe80::1%eth0",
        "192.0.2.1, bad",
        "192.0.2.1,",
        ["192.0.2.1", "198.51.100.1"],
        ",".join(["192.0.2.1"] * 17),
        "x" * 2049,
    ],
)
def test_invalid_missing_duplicate_or_excessive_chains_are_unknown(forwarded):
    value = _resolve("127.0.0.1", forwarded)
    assert value.address is None
    assert value.source == "unknown"


def test_unknown_raw_peer():
    assert _resolve(None, "192.0.2.1").source == "unknown"
    assert _resolve("testclient", "192.0.2.1").source == "unknown"
