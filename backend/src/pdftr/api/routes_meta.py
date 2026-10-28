"""健康检查等元信息接口。"""

import os

from fastapi import APIRouter, Request

from pdftr import __version__
from pdftr.api.schemas import HealthOut

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    data_dir = request.app.state.container.settings.data_dir
    return HealthOut(status="ok", version=__version__, data_dir_writable=os.access(data_dir, os.W_OK))
