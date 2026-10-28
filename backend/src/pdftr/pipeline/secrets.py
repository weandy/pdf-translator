"""任务级密钥：只在内存里，任务结束即清除（docs/01-tech-stack.md §4.4.2）。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Secrets:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    parser_token: str  # MinerU Token
    paddle_token: str = ""  # PaddleOCR（星河社区）Token

    def token_for(self, parser: str) -> str:
        return self.paddle_token if parser == "paddleocr-vl" else self.parser_token

    def __repr__(self) -> str:  # 防止密钥被打进日志
        return f"Secrets(llm_base_url={self.llm_base_url!r}, llm_model={self.llm_model!r}, keys=***)"


class SecretStore:
    def __init__(self) -> None:
        self._data: dict[str, Secrets] = {}

    def put(self, job_id: str, secrets: Secrets) -> None:
        self._data[job_id] = secrets

    def get(self, job_id: str) -> Secrets | None:
        return self._data.get(job_id)

    def drop(self, job_id: str) -> None:
        self._data.pop(job_id, None)
