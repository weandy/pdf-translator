"""健康检查、公开配置、费用预估。"""

import os
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from pdftr import __version__
from pdftr.api.schemas import ConfigOut, HealthOut
from pdftr.glossary.gist import ATTRIBUTION as GIST_ATTRIBUTION
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
        attributions=["MinerU (https://github.com/opendatalab/MinerU)", "PyMuPDF (AGPL-3.0)", GIST_ATTRIBUTION],
    )


@router.get("/estimate", response_model=usecases.Estimate)
def estimate(
    request: Request,
    pages: Annotated[int, Query(ge=1, le=2000)],
    quality: Literal["default", "careful"] = "default",
    model: str | None = None,
) -> usecases.Estimate:
    return usecases.estimate(pages, quality, model or request.app.state.container.settings.llm_model)
