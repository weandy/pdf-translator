"""术语来源端口：词库（如 GIST）按英文术语查中文译法。"""

from collections.abc import Iterable
from typing import Protocol


class TermSource(Protocol):
    name: str

    def lookup(self, terms: Iterable[str]) -> dict[str, str]: ...
