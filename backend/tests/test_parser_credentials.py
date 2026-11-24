"""站点解析 Token 的回退、个人覆盖及公开配置的保密边界。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import Container, build_container


@pytest.fixture
def container(tmp_path: Path) -> Iterator[Container]:
    c = build_container(
        Settings(
            data_dir=tmp_path,
            glossary_sources="",
            mineru_api_token="server-parser-for-test",
            llm_api_key="server-llm-for-test",
            paddle_token="server-paddle-for-test",
        )
    )
    try:
        yield c
    finally:
        c.pipeline.deps.store.close()  # type: ignore[attr-defined]
        c.pipeline.deps.cache.close()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("personal_token", "expected"),
    [
        (None, "server-parser-for-test"),
        ("", "server-parser-for-test"),
        ("personal-parser-for-test", "personal-parser-for-test"),
    ],
)
def test_parser_token_uses_personal_value_or_site_default(
    container: Container, personal_token: str | None, expected: str
):
    credentials = container.secrets(base_url=None, api_key=None, model=None, parser_token=personal_token)
    assert credentials.parser_token == expected


def test_parser_token_is_empty_when_neither_source_is_configured(container: Container):
    container.settings.mineru_api_token = ""
    credentials = container.secrets(base_url=None, api_key=None, model=None, parser_token=None)
    assert credentials.parser_token == ""


@pytest.mark.parametrize("site_token", ["", "server-parser-for-test"])
def test_public_config_exposes_only_token_availability(container: Container, site_token: str):
    container.settings.mineru_api_token = site_token
    with TestClient(create_app(container)) as client:
        response = client.get(
            "/api/config", headers={"X-Parser-Key": "personal-parser-for-test", "X-LLM-Key": "personal-llm-for-test"}
        )
    assert response.status_code == 200
    config = response.json()
    assert config["server_has_parser_token"] is bool(site_token)
    assert config["server_has_llm_key"] is True
    assert config["server_has_paddle_token"] is True
    for secret in (
        "server-parser-for-test",
        "server-llm-for-test",
        "server-paddle-for-test",
        "personal-parser-for-test",
        "personal-llm-for-test",
    ):
        assert secret not in response.text
    assert "mineru_api_token" not in config
    assert "parser_token" not in config
    assert config["default_mineru_base_url"] == "https://mineru.net"


def test_personal_parser_key_base_url_binding_and_free_mode_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    import json
    import os
    import socket

    import pymupdf

    from pdftr.domain.admin import hash_password
    from pdftr.domain.layout import LayoutResult
    from pdftr.domain.llm import ChatOptions, ChatResult

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    doc = pymupdf.open()
    doc.new_page(width=400, height=400).insert_text((50, 50), "Hello", fontsize=10)
    pdf_bytes = doc.tobytes()
    doc.close()

    class _NoopParser:
        name = "noop"
        is_cloud = True

        async def parse(self, pdf: Path, pages=None, raw_dir=None, on_progress=None) -> LayoutResult:
            del pdf, pages, raw_dir, on_progress
            return LayoutResult(parser="noop", pages=[])

    class _NoopChat:
        model = "m"

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            del system, user, opts
            return ChatResult(text="1|译")

        async def list_models(self) -> list[str]:
            return ["m"]

    origin = "https://admin.test"
    pw = "synthetic-binding-pass-2026"
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            admin_password_hash=hash_password(pw),
            admin_origin=origin,
            config_secret_key=key,
        ),
        deps_override={
            "parser_factory": lambda sec, name: _NoopParser(),
            "chat_factory": lambda sec: _NoopChat(),
        },
    )
    try:
        with TestClient(create_app(c), base_url=origin) as client:
            # 1. 官方默认地址时：不带 X-Parser-Base-URL 兼容放行，带不匹配地址返回 400
            assert client.get("/api/config").json()["default_mineru_base_url"] == "https://mineru.net"
            mismatch = client.post(
                "/api/jobs",
                files={"file": ("a.pdf", pdf_bytes, "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers={
                    "X-LLM-Key": "sk-user",
                    "X-Parser-Key": "personal-mineru-tok",
                    "X-Parser-Base-URL": "https://other.example.com",
                },
            )
            assert mismatch.status_code == 400
            assert "MinerU 服务地址已变更" in mismatch.json()["detail"]

            compat_ok = client.post(
                "/api/jobs",
                files={"file": ("b.pdf", pdf_bytes, "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers={"X-LLM-Key": "sk-user", "X-Parser-Key": "personal-mineru-tok"},
            )
            assert compat_ok.status_code == 202

            # 2. 管理员修改 MinerU 服务地址为非官方地址并配置免费渠道
            login = client.post("/api/admin/session", json={"password": pw}, headers={"Origin": origin})
            admin_headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
            custom_mineru = "https://custom-mineru.example.com"
            r_p = client.put(
                "/api/admin/config/parser",
                json={
                    "expected_revision": 0,
                    "mineru_base_url": custom_mineru,
                    "mineru_token": {"action": "replace", "value": "site-custom-tok-123456"},
                },
                headers=admin_headers,
            )
            assert r_p.status_code == 200
            r_f = client.put(
                "/api/admin/config/free_translation",
                json={
                    "expected_revision": 1,
                    "enabled": True,
                    "providers": [
                        {
                            "id": "p1",
                            "name": "免费线",
                            "enabled": True,
                            "protocol": "openai",
                            "base_url": "https://free.example.com/v1",
                            "model": "m1",
                            "api_key": {"action": "replace", "value": "sk-free-123456"},
                        }
                    ],
                },
                headers=admin_headers,
            )
            assert r_f.status_code == 200
            assert client.get("/api/config").json()["default_mineru_base_url"] == custom_mineru

            # 非官方地址时，携带个人 X-Parser-Key 但缺 X-Parser-Base-URL 必须返回 400
            missing_hdr = client.post(
                "/api/jobs",
                files={"file": ("c.pdf", pdf_bytes, "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers={"X-LLM-Key": "sk-user", "X-Parser-Key": "personal-official-tok"},
            )
            assert missing_hdr.status_code == 400
            assert "自定义 MinerU 服务地址" in missing_hdr.json()["detail"]

            # 携带匹配的 X-Parser-Base-URL（含尾部斜杠/默认端口等价）正常放行
            matched_ok = client.post(
                "/api/jobs",
                files={"file": ("d.pdf", pdf_bytes, "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers={
                    "X-LLM-Key": "sk-user",
                    "X-Parser-Key": "personal-custom-tok",
                    "X-Parser-Base-URL": f"{custom_mineru}:443/",
                },
            )
            assert matched_ok.status_code == 202

            # 免费模式即使误带 X-Parser-Key 和不匹配的 X-Parser-Base-URL，后端彻底忽略仍放行
            free_ok = client.post(
                "/api/jobs",
                files={"file": ("e.pdf", pdf_bytes, "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers={
                    "X-Translation-Mode": "free",
                    "X-Parser-Key": "personal-should-be-ignored",
                    "X-Parser-Base-URL": "https://wrong-host.example.com",
                },
            )
            assert free_ok.status_code == 202
    finally:
        c.close()
