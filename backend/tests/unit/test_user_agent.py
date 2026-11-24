"""User-Agent 解析单元测试：覆盖主流桌面/移动/平板浏览器、微信、自动化工具与异常输入清洗。"""

import pytest

from pdftr.domain.user_agent import ClientInfo, parse_user_agent


@pytest.mark.parametrize(
    ("raw", "expected_device", "expected_os", "expected_browser"),
    [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "desktop",
            "Windows",
            "Chrome",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
            "desktop",
            "Windows",
            "Edge",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
            "desktop",
            "macOS",
            "Safari",
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
            "desktop",
            "Linux",
            "Firefox",
        ),
        (
            "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "desktop",
            "Linux",
            "Chrome",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
            "mobile",
            "iOS",
            "Safari",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1",
            "mobile",
            "iOS",
            "Chrome",
        ),
        (
            "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
            "tablet",
            "iOS",
            "Safari",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
            "mobile",
            "Android",
            "Chrome",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; SM-X910) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "tablet",
            "Android",
            "Chrome",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50(0x18003237) NetType/WIFI Language/zh_CN",
            "mobile",
            "iOS",
            "WeChat",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; 23116PN5BC Build/UKQ1.230804.001; wv) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.188 Mobile Safari/537.36 "
            "MicroMessenger/8.0.50.2701",
            "mobile",
            "Android",
            "WeChat",
        ),
        ("curl/8.5.0", "bot", "Unknown", "curl"),
        ("python-httpx/0.27.2", "bot", "Unknown", "python-httpx"),
        ("python-requests/2.32.3", "bot", "Unknown", "python-requests"),
        ("Python-urllib/3.12", "bot", "Unknown", "urllib"),
        ("Wget/1.21.4", "bot", "Unknown", "wget"),
        ("PostmanRuntime/7.39.0", "bot", "Unknown", "Postman"),
        ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "bot", "Unknown", "Bot"),
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) HeadlessChrome/131.0.0.0 Safari/537.36",
            "bot",
            "Linux",
            "HeadlessChrome",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Playwright/1.48.0",
            "bot",
            "Windows",
            "Playwright",
        ),
    ],
)
def test_parse_user_agent_classifications(
    raw: str,
    expected_device: str,
    expected_os: str,
    expected_browser: str,
) -> None:
    info = parse_user_agent(raw)
    assert info.device == expected_device
    assert info.os == expected_os
    assert info.browser == expected_browser
    assert info.raw == raw


@pytest.mark.parametrize("raw", [None, "", "   \t\r\n  ", "\x00\x1f\u200b"])
def test_parse_user_agent_empty_returns_defaults(raw: str | None) -> None:
    assert parse_user_agent(raw) == ClientInfo(raw="", device="unknown", os="Unknown", browser="Unknown")


def test_parse_user_agent_strips_control_chars_and_normalizes_whitespace() -> None:
    raw = "\x00\u200bMozilla/5.0\t(Windows NT 10.0)\r\n  Chrome/131.0.0.0\x1f  Safari/537.36\u200c"
    info = parse_user_agent(raw)
    assert info.raw == "Mozilla/5.0 (Windows NT 10.0) Chrome/131.0.0.0 Safari/537.36"
    assert info.device == "desktop"
    assert info.os == "Windows"
    assert info.browser == "Chrome"


def test_parse_user_agent_truncates_overlong_string_to_512_chars() -> None:
    prefix = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15 "
    raw = prefix + ("X" * 1000)
    info = parse_user_agent(raw)
    assert len(info.raw) == 512
    assert info.raw.startswith(prefix)
    assert info.device == "desktop"
    assert info.os == "macOS"
    assert info.browser == "Safari"
