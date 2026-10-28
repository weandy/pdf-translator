# 字体：思源宋体/思源黑体/Tinos，精简到 GB 2312 + 西文（见 scripts/fetch_fonts.py）。
# 单独一个构建阶段，fontTools 不进入运行镜像；脚本不变时这一层会被缓存。
FROM python:3.12-slim AS fonts
RUN pip install --no-cache-dir fonttools==4.60.1 brotli==1.1.0
COPY scripts/fetch_fonts.py /tmp/fetch_fonts.py
RUN python /tmp/fetch_fonts.py /fonts

# 不在镜像里留下 uv 下载缓存（M6 实测：缓存曾让镜像多出约 345 MB）
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

WORKDIR /app

# 先只装依赖，利用镜像层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

COPY --from=fonts /fonts ./fonts

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    PDFTR_DATA_DIR=/data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

# 停止时只等 10 秒让连接关闭（进度推送是长连接，不会自己结束），之后在 lifespan 里等待进行中的任务
CMD ["uvicorn", "pdftr.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10"]
