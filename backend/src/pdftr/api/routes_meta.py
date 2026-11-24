"""健康检查、公开配置、费用预估、阅读器字体。"""

import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse

from pdftr import __version__
from pdftr.api.deps import container, pipeline, secrets
from pdftr.api.schemas import ConfigOut, FreeChannelPublicOut, HealthOut, ModelsOut
from pdftr.domain.runtime_config import default_llm_presets
from pdftr.glossary.sources import SOURCES, parse_ids
from pdftr.glossary.user_import import ParsedGlossary
from pdftr.pipeline import usecases
from pdftr.pipeline.runner import Pipeline
from pdftr.pipeline.secrets import Secrets

router = APIRouter(prefix="/api", tags=["meta"])
PipelineDep = Annotated[Pipeline, Depends(pipeline)]
SecretsDep = Annotated[Secrets, Depends(secrets)]


@router.get("/llm/models", response_model=ModelsOut)
async def llm_models(p: PipelineDep, sec: SecretsDep) -> ModelsOut:
    """用请求头里的接口地址、协议与 Key 拉取模型列表（浏览器不能直接跨域请求翻译服务）。"""
    return ModelsOut(models=await usecases.llm_models(p, sec))


@router.post("/glossary/parse", response_model=ParsedGlossary)
async def parse_glossary(
    p: PipelineDep, sec: SecretsDep, file: Annotated[UploadFile, File()], ai: Annotated[bool, Form()] = False
) -> ParsedGlossary:
    """识别上传的术语表，返回候选条目给用户确认（服务端不保存）。ai=true 时用翻译服务读取任意格式。"""
    return await usecases.parse_glossary(p, sec, await file.read(usecases.GLOSSARY_MAX_BYTES + 1), use_ai=ai)


@router.post("/llm/check", response_model=usecases.LlmCheck)
async def llm_check(p: PipelineDep, sec: SecretsDep) -> usecases.LlmCheck:
    """测试连接：用请求头里的设置发一个很短的请求。"""
    return await usecases.llm_check(p, sec)


@router.get("/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    data_dir = container(request).settings.data_dir
    return HealthOut(status="ok", version=__version__, data_dir_writable=os.access(data_dir, os.W_OK))


@router.get("/config", response_model=ConfigOut)
def config(request: Request) -> ConfigOut:
    c = container(request)
    s = c.settings
    snap = c.runtime_config.snapshot() if c.runtime_config is not None else None
    if snap is None:
        return ConfigOut(
            version=__version__,
            default_base_url=s.llm_base_url,
            default_model=s.llm_model,
            default_protocol=s.llm_protocol,
            default_mineru_base_url="https://mineru.net",
            server_has_llm_key=bool(s.llm_api_key),
            server_has_parser_token=bool(s.mineru_api_token),
            server_has_paddle_token=bool(s.paddle_token),
            max_upload_mb=s.max_upload_mb,
            max_pages=s.max_pages,
            retention_hours=s.retention_hours,
            admin_path=s.admin_path,
            submission_audit_enabled=s.submission_audit_enabled,
            attributions=[
                "MinerU (https://github.com/opendatalab/MinerU)",
                "PyMuPDF (AGPL-3.0)",
                "思源宋体、思源黑体、Tinos（SIL OFL 1.1）",
                *(f"术语库：{src.attribution}" for src in parse_ids(s.glossary_sources)),
            ],
            llm_presets=default_llm_presets(),
        )

    use_hot_system = snap.revision > 0
    sys_cfg = snap.data.system
    free_ready = snap.free_channel_ready()
    active_free = snap.active_free_providers()
    glossary_items = [SOURCES[sid] for sid in snap.data.glossary.sources if sid in SOURCES]
    same_env_default_llm = (
        snap.data.default_llm.base_url.rstrip("/") == s.llm_base_url.rstrip("/")
        and snap.data.default_llm.protocol == s.llm_protocol
    )
    return ConfigOut(
        version=__version__,
        default_base_url=snap.data.default_llm.base_url,
        default_model=snap.data.default_llm.model,
        default_protocol=snap.data.default_llm.protocol,
        default_mineru_base_url=snap.data.parser.mineru_base_url.rstrip("/"),
        server_has_llm_key=bool(s.llm_api_key) and same_env_default_llm,
        server_has_parser_token=bool(snap.mineru_token.value) and not snap.mineru_token.corrupted,
        server_has_paddle_token=bool(snap.paddle_token.value) and not snap.paddle_token.corrupted,
        max_upload_mb=sys_cfg.max_upload_mb if use_hot_system else s.max_upload_mb,
        max_pages=sys_cfg.max_pages if use_hot_system else s.max_pages,
        retention_hours=sys_cfg.retention_hours if use_hot_system else s.retention_hours,
        admin_path=s.admin_path,
        submission_audit_enabled=s.submission_audit_enabled,
        attributions=[
            "MinerU (https://github.com/opendatalab/MinerU)",
            "PyMuPDF (AGPL-3.0)",
            "思源宋体、思源黑体、Tinos（SIL OFL 1.1）",
            *(f"术语库：{src.attribution}" for src in glossary_items),
        ],
        free_channel=FreeChannelPublicOut(
            enabled=free_ready,
            model_display=(active_free[0].config.name or active_free[0].config.model)
            if free_ready and active_free
            else "",
            max_pages=snap.data.free_translation.max_pages,
            notice=snap.data.free_translation.notice,
        ),
        llm_presets=snap.data.default_llm.presets,
        paddle_public_enabled=snap.data.parser.paddle_public_enabled,
        maintenance_mode=sys_cfg.maintenance_mode,
        site_notice=sys_cfg.site_notice,
    )


# 阅读器与导出 PDF 用同一套字体（GB/T 7713.2：正文宋体、标题黑体、西文 Times 类），见 rendering/fonts.py
WebFont = Literal[
    "NotoSerifSC-Regular.woff2", "NotoSerifSC-Bold.woff2", "NotoSansSC-Medium.woff2",
    "NotoSansSC-Bold.woff2", "Tinos-Regular.woff2", "Tinos-Bold.woff2",
    "Tinos-Italic.woff2", "Tinos-BoldItalic.woff2",
]  # fmt: skip


@router.get("/fonts/{name}")
def font(name: WebFont, request: Request) -> FileResponse:
    path = container(request).settings.font_dir / name
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
    c = container(request)
    snap = c.runtime_config.snapshot() if c.runtime_config is not None else None
    default_model = snap.data.default_llm.model if snap is not None else c.settings.llm_model
    prices = snap.price_overrides if snap is not None else None
    return usecases.estimate(pages, quality, model or default_model, prices=prices)
