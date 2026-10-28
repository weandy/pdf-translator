"""组合根：在这里创建适配器并注入，其他模块只依赖端口。

后续里程碑新增的解析器、大模型适配器、仓储都在这里注册。
"""

from dataclasses import dataclass

from pdftr.config import Settings
from pdftr.storage.files import FileArtifactStore
from pdftr.storage.port import ArtifactStore


@dataclass(frozen=True)
class Container:
    settings: Settings
    artifacts: ArtifactStore


def build_container(settings: Settings | None = None) -> Container:
    s = settings or Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return Container(
        settings=s,
        artifacts=FileArtifactStore(s.data_dir),
    )
