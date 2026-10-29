"""翻译模块依赖的外部能力。大模型端口见 domain/llm.py。"""

from typing import Protocol


class TranslationCache(Protocol):
    """译文缓存：键由原文、公式、模型、提示词版本、术语表、档位共同决定（见 translation/service.py）。"""

    def get(self, key: str) -> str | None: ...

    def put(self, key: str, target: str) -> None: ...

    def purge_older_than(self, hours: int) -> int: ...
