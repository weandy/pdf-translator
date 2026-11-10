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
