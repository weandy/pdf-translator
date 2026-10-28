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

    max_concurrent_jobs: int = 2
    max_queued_jobs: int = 10  # 排队中的任务超过这个数时拒绝新上传
    retention_days: int = 7  # 任务文件与译文缓存保留天数，0 表示不自动清理
    min_free_disk_mb: int = 1024  # 磁盘剩余空间低于此值时拒绝新上传
    gist_enabled: bool = True  # 首次使用时下载 GIST AI 术语库（Apache-2.0）

    cors_origins: list[str] = []  # 开发时前端单独运行可填 ["http://localhost:5173"]
