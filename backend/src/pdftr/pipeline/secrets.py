"""任务级密钥：只在内存里，任务结束即清除（docs/01-tech-stack.md §4.4.2）。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Secrets:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    parser_token: str  # MinerU Token
    paddle_token: str = ""  # PaddleOCR（星河社区）Token
    llm_protocol: str = "openai"  # openai（OpenAI 兼容）或 anthropic（Anthropic Messages）
    channel_mode: Literal["byok", "free"] = "byok"
    channel_provider_id: str = ""
    fallback_models: tuple[str, ...] = ()
    # 跨上游备选端点：(protocol, base_url, api_key, model, timeout_s)
    fallback_endpoints: tuple[tuple[str, str, str, str, int], ...] = ()
    llm_timeout_s: int | None = None
    mineru_base_url: str = "https://mineru.net"
    mineru_proxy: str = ""
    paddle_proxy: str = ""
    disable_redirects: bool = True
    free_sdk_no_retry: bool = False
    allow_private_urls: bool = False

    def token_for(self, parser: str) -> str:
        return self.paddle_token if parser == "paddleocr-vl" else self.parser_token

    def __repr__(self) -> str:  # 防止密钥、Token 或代理账密被打进日志
        return (
            f"Secrets(channel_mode={self.channel_mode!r}, llm_base_url={self.llm_base_url!r}, "
            f"llm_model={self.llm_model!r}, keys=***)"
        )


class SecretStore:
    def __init__(self) -> None:
        self._data: dict[str, Secrets] = {}

    def put(self, job_id: str, secrets: Secrets) -> None:
        self._data[job_id] = secrets

    def get(self, job_id: str) -> Secrets | None:
        return self._data.get(job_id)

    def drop(self, job_id: str) -> None:
        self._data.pop(job_id, None)
