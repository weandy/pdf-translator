"""FastAPI 应用工厂。

启动：uvicorn pdftr.api.app:create_app --factory
"""

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from pdftr import __version__
from pdftr.api import routes_meta
from pdftr.container import Container, build_container

# 接口只返回 JSON / 文件，不需要加载任何脚本或样式
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def create_app(container: Container | None = None) -> FastAPI:
    c = container or build_container()
    app = FastAPI(title="pdf-translator", version=__version__)
    app.state.container = c

    if c.settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=c.settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    app.include_router(routes_meta.router)
    return app
