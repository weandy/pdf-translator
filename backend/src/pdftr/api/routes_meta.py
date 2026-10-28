"""健康检查、公开配置、费用预估、阅读器字体。"""

import os
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

from pdftr import __version__
from pdftr.api.schemas import ConfigOut, HealthOut
from pdftr.glossary.sources import parse_ids
from pdftr.pipeline import usecases

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    data_dir = request.app.state.container.settings.data_dir
    return HealthOut(status="ok", version=__version__, data_dir_writable=os.access(data_dir, os.W_OK))


@router.get("/config", response_model=ConfigOut)
def config(request: Request) -> ConfigOut:
    s = request.app.state.container.settings
    return ConfigOut(
        version=__version__,
        default_base_url=s.llm_base_url,
        default_model=s.llm_model,
        server_has_llm_key=bool(s.llm_api_key),
        server_has_parser_token=bool(s.mineru_api_token),
        max_upload_mb=s.max_upload_mb,
        max_pages=s.max_pages,
        retention_days=s.retention_days,
        attributions=[
            "MinerU (https://github.com/opendatalab/MinerU)",
            "PyMuPDF (AGPL-3.0)",
            "思源宋体、思源黑体、Tinos（SIL OFL 1.1）",
            *(f"术语库：{src.attribution}" for src in parse_ids(s.glossary_sources)),
        ],
    )


# 阅读器与导出 PDF 用同一套字体（GB/T 7713.2：正文宋体、标题黑体、西文 Times 类），见 rendering/fonts.py
WebFont = Literal["NotoSerifSC-Regular.woff2", "NotoSansSC-Medium.woff2", "Tinos-Regular.woff2", "Tinos-Bold.woff2"]


@router.get("/fonts/{name}")
def font(name: WebFont, request: Request) -> FileResponse:
    path = request.app.state.container.settings.font_dir / name
    if not path.is_file():
        raise usecases.NotFound("字体文件未安装")
    # 文件名不变时内容不变；升级字体时前端带版本号参数刷新
    return FileResponse(path, media_type="font/woff2", headers={"Cache-Control": "public, max-age=2592000"})


@router.get("/estimate", response_model=usecases.Estimate)
def estimate(
    request: Request,
    pages: Annotated[int, Query(ge=1, le=2000)],
    quality: Literal["default", "careful"] = "default",
    model: str | None = None,
) -> usecases.Estimate:
    return usecases.estimate(pages, quality, model or request.app.state.container.settings.llm_model)
