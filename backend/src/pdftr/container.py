"""组合根：在这里创建适配器并注入，其他模块只依赖端口。"""

import logging
import threading
from dataclasses import dataclass

from pdftr.config import Settings
from pdftr.glossary import gist
from pdftr.glossary.matcher import TermMatcher
from pdftr.parsing.mineru_cloud import MinerUCloudParser
from pdftr.pipeline.events import EventBus
from pdftr.pipeline.runner import Pipeline, PipelineDeps
from pdftr.pipeline.secrets import Secrets, SecretStore
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.files import FileArtifactStore
from pdftr.storage.sqlite import SqliteStore
from pdftr.translation.openai_compat import OpenAICompatChat

log = logging.getLogger(__name__)


class GistLoader:
    """首次需要时下载并加载 GIST 词库；失败时不影响翻译，只是少了词库术语。"""

    def __init__(self, settings: Settings) -> None:
        self._file = settings.data_dir / "glossaries" / "vendor" / "gist_zh.csv"
        self._enabled = settings.gist_enabled
        self._lock = threading.Lock()
        self._matcher: TermMatcher | None = None
        self._tried = False

    def __call__(self) -> TermMatcher | None:
        with self._lock:
            if self._tried or not self._enabled:
                return self._matcher
            self._tried = True
            try:
                if not self._file.exists():
                    gist.download(self._file)
                self._matcher = TermMatcher(gist.load(self._file))
            except Exception:
                log.warning("GIST 词库加载失败，本次不使用词库术语", exc_info=True)
            return self._matcher


@dataclass(frozen=True)
class Container:
    settings: Settings
    pipeline: Pipeline

    def secrets(
        self, *, base_url: str | None, api_key: str | None, model: str | None, parser_token: str | None
    ) -> Secrets:
        """请求头里的 Key（来自用户浏览器）优先，其次是部署者在 .env 配置的共享 Key。"""
        s = self.settings
        return Secrets(
            llm_base_url=base_url or s.llm_base_url,
            llm_api_key=api_key or s.llm_api_key,
            llm_model=model or s.llm_model,
            parser_token=parser_token or s.mineru_api_token,
        )


def build_container(settings: Settings | None = None, deps_override: dict | None = None) -> Container:
    s = settings or Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    db = s.data_dir / "app.db"
    deps = {
        "artifacts": FileArtifactStore(s.data_dir),
        "store": SqliteStore(db),
        "cache": SqliteTranslationCache(db),
        "events": EventBus(),
        "secrets": SecretStore(),
        "parser_factory": lambda sec: MinerUCloudParser(sec.parser_token),
        "chat_factory": lambda sec: OpenAICompatChat(
            base_url=sec.llm_base_url, api_key=sec.llm_api_key, model=sec.llm_model
        ),
        "term_matcher": GistLoader(s),
        "max_concurrent_jobs": s.max_concurrent_jobs,
    } | (deps_override or {})  # 测试时替换解析器、大模型等
    return Container(settings=s, pipeline=Pipeline(PipelineDeps(**deps)))
