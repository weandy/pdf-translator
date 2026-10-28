"""运行配置：全部来自环境变量（前缀 PDFTR_），见仓库根目录 .env.example。"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PDFTR_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    max_upload_mb: int = 200
    max_pages: int = 200  # MinerU 精准解析单文件上限（S2 / 调研 §4.1）

    # 部署者可选配置的共享 Key；用户在浏览器里填写的 Key 优先（docs/01-tech-stack.md §4.4.2）
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_api_key: str = ""
    mineru_api_token: str = ""

    llm_concurrency: int = 0  # 每个任务同时发给翻译服务的请求数；0 = 按服务商自动（DeepSeek 4、本机模型 1、其他 2）
    max_concurrent_jobs: int = 2
    max_queued_jobs: int = 10  # 排队中的任务超过这个数时拒绝新上传
    retention_days: int = 7  # 任务文件与译文缓存保留天数，0 表示不自动清理
    min_free_disk_mb: int = 1024  # 磁盘剩余空间低于此值时拒绝新上传
    # 论文术语库来源，逗号分隔，排在前面的优先；首次使用时下载（见 glossary/sources.py）。
    # 机器之心术语库（aitd）为 CC BY-NC-SA 4.0，不可商用：商业部署请去掉 aitd；留空则不用术语库
    glossary_sources: str = "aitd,d2l,ml-technical-term,ais-vocab,gist,computerese"

    font_dir: Path = Path("fonts")  # 导出 PDF 与网页阅读器用的字体（scripts/fetch_fonts.py 生成）
    cors_origins: list[str] = []  # 开发时前端单独运行可填 ["http://localhost:5173"]
