"""轻量 User-Agent 解析：仅提取设备类型、操作系统与浏览器，零第三方依赖。"""

import unicodedata
from typing import Literal

from pydantic import BaseModel

ClientDevice = Literal["desktop", "mobile", "tablet", "bot", "unknown"]

_MAX_UA_LENGTH = 512

_BOT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("python-httpx", "python-httpx"),
    ("python-requests", "python-requests"),
    ("curl/", "curl"),
    ("wget", "wget"),
    ("postman", "Postman"),
    ("urllib", "urllib"),
    ("playwright", "Playwright"),
    ("headlesschrome", "HeadlessChrome"),
    ("crawler", "Bot"),
    ("spider", "Bot"),
    ("bot", "Bot"),
)


class ClientInfo(BaseModel):
    raw: str = ""
    device: ClientDevice = "unknown"
    os: str = "Unknown"
    browser: str = "Unknown"


def _clean_user_agent(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = "".join(c for c in raw if unicodedata.category(c) not in {"Cc", "Cf"} or c.isspace())
    return " ".join(cleaned.split())[:_MAX_UA_LENGTH]


def _detect_bot(lower: str) -> str | None:
    if lower == "curl":
        return "curl"
    for token, name in _BOT_PATTERNS:
        if token in lower:
            return name
    return None


def _detect_os(lower: str, *, fallback: str) -> str:
    # iPhone/iPad UA 包含 "like Mac OS X"，Android UA 包含 "Linux"，必须优先判定。
    if any(token in lower for token in ("iphone", "ipad", "ipod", "cpu iphone os", "cpu os")):
        return "iOS"
    if "android" in lower:
        return "Android"
    if "windows" in lower:
        return "Windows"
    if any(token in lower for token in ("macintosh", "mac os x", "macos")):
        return "macOS"
    if any(token in lower for token in ("cros", "linux", "x11", "ubuntu", "fedora")):
        return "Linux"
    return fallback


def _detect_browser(lower: str) -> str:
    # 微信、Edge、Chrome UA 中常混带 Safari/Chrome 字样，按特异性从高到低匹配。
    if "micromessenger" in lower or "wechat" in lower:
        return "WeChat"
    if any(token in lower for token in ("edg/", "edga/", "edgios/", "edge/")):
        return "Edge"
    if "firefox/" in lower or "fxios/" in lower:
        return "Firefox"
    if "chrome/" in lower or "crios/" in lower:
        return "Chrome"
    if "safari/" in lower:
        return "Safari"
    return "Other"


def _detect_device(lower: str, os_name: str) -> ClientDevice:
    if "ipad" in lower or "tablet" in lower or ("android" in lower and "mobile" not in lower):
        return "tablet"
    if any(token in lower for token in ("iphone", "ipod", "mobile")) or ("android" in lower and "mobile" in lower):
        return "mobile"
    if os_name in {"Windows", "macOS", "Linux"} or "cros" in lower:
        return "desktop"
    return "unknown"


def parse_user_agent(raw: str | None) -> ClientInfo:
    """解析 User-Agent 字符串为规范化客户端摘要。"""
    cleaned = _clean_user_agent(raw)
    if not cleaned:
        return ClientInfo()

    lower = cleaned.lower()
    bot_name = _detect_bot(lower)
    if bot_name is not None:
        return ClientInfo(
            raw=cleaned,
            device="bot",
            os=_detect_os(lower, fallback="Unknown"),
            browser=bot_name,
        )

    os_name = _detect_os(lower, fallback="Other")
    browser_name = _detect_browser(lower)
    device = _detect_device(lower, os_name)
    return ClientInfo(raw=cleaned, device=device, os=os_name, browser=browser_name)
