import pytest
from fastapi import HTTPException

from pdftr.api.deps import validate_user_llm_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.8/v1",
        "https://localhost/v1",
    ],
)
def test_custom_llm_url_rejects_private_targets(url: str) -> None:
    with pytest.raises(HTTPException, match=r"不能访问内网(?:域名)?"):
        validate_user_llm_url(url)


def test_custom_llm_url_accepts_public_ip_and_explicit_private_mode() -> None:
    assert validate_user_llm_url("https://8.8.8.8/v1") == "https://8.8.8.8/v1"
    assert validate_user_llm_url("http://localhost:11434/v1", allow_private=True) == "http://localhost:11434/v1"


def test_custom_llm_url_rejects_credentials_and_query() -> None:
    with pytest.raises(HTTPException):
        validate_user_llm_url("https://user:pass@8.8.8.8/v1")
    with pytest.raises(HTTPException):
        validate_user_llm_url("https://8.8.8.8/v1?redirect=secret")
