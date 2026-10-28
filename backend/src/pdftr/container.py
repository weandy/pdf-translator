"""组合根：在这里创建适配器并注入，其他模块只依赖端口。"""

import logging
import threading
import time
from dataclasses import dataclass

from pdftr.config import Settings
from pdftr.glossary import sources as glossary_sources
from pdftr.glossary.matcher import TermMatcher
from pdftr.parsing.mineru_cloud import MinerUCloudParser
from pdftr.parsing.paddle_vl import PaddleVLParser
from pdftr.pipeline.events import EventBus
from pdftr.pipeline.runner import Pipeline, PipelineDeps
from pdftr.pipeline.secrets import Secrets, SecretStore
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.files import FileArtifactStore
from pdftr.storage.sqlite import SqliteStore
from pdftr.translation.openai_compat import OpenAICompatChat

log = logging.getLogger(__name__)


class GlossaryLoader:
    """下载并合并各术语库来源；某个来源失败只跳过它，不影响翻译，30 分钟后再试。

    服务启动时在后台预先加载（见 api/app.py），第一个任务不用等下载。
    """

    RETRY_AFTER_S = 1800

    def __init__(self, settings: Settings) -> None:
        self._vendor = settings.data_dir / "glossaries" / "vendor"
        self._sources = glossary_sources.parse_ids(settings.glossary_sources)
        self._lock = threading.Lock()
        self._matcher: TermMatcher | None = None
        self._retry_at: float | None = 0.0  # None = 全部加载成功，不再重试

    def __call__(self) -> TermMatcher | None:
        with self._lock:
            if not self._sources or self._retry_at is None or time.monotonic() < self._retry_at:
                return self._matcher
            terms, loaded = glossary_sources.load_all(self._sources, self._vendor)
            if terms:
                self._matcher = TermMatcher(terms)
                log.info("术语库：%s，共 %d 条", "、".join(s.id for s in loaded), len(terms))
            complete = len(loaded) == len(self._sources)
            self._retry_at = None if complete else time.monotonic() + self.RETRY_AFTER_S
            return self._matcher


@dataclass(frozen=True)
class Container:
    settings: Settings
    pipeline: Pipeline

    def secrets(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        model: str | None,
        parser_token: str | None,
        paddle_token: str | None = None,
    ) -> Secrets:
        """请求头里的 Key（来自用户浏览器）优先，其次是部署者在 .env 配置的共享 Key。"""
        s = self.settings
        return Secrets(
            llm_base_url=base_url or s.llm_base_url,
            llm_api_key=api_key or s.llm_api_key,
            llm_model=model or s.llm_model,
            parser_token=parser_token or s.mineru_api_token,
            paddle_token=paddle_token or s.paddle_token,
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
        "parser_factory": lambda sec, name: (
            PaddleVLParser(sec.paddle_token, proxy=s.paddle_proxy)
            if name == "paddleocr-vl"
            else MinerUCloudParser(sec.parser_token, proxy=s.mineru_proxy)
        ),
        "chat_factory": lambda sec: OpenAICompatChat(
            base_url=sec.llm_base_url, api_key=sec.llm_api_key, model=sec.llm_model
        ),
        "term_matcher": GlossaryLoader(s),
        "max_concurrent_jobs": s.max_concurrent_jobs,
        "font_dir": s.font_dir,
        "llm_concurrency": s.llm_concurrency,
    } | (deps_override or {})  # 测试时替换解析器、大模型等
    return Container(settings=s, pipeline=Pipeline(PipelineDeps(**deps)))
