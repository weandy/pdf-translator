"""接口层的依赖：容器、来自请求头的密钥与双模式凭据解析。"""

from typing import Literal
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from pdftr.container import Container
from pdftr.domain.job import Job
from pdftr.pipeline.runner import Pipeline
from pdftr.pipeline.secrets import Secrets
from pdftr.pipeline.url_guard import validate_llm_url

_DEFAULT_MINERU_URL = "https://mineru.net"


def _normalize_parser_base_url(raw: str) -> str | None:
    """规范化 MinerU 服务基础地址（统一大小写、省略默认端口与尾部斜杠），用于目标绑定比对。"""
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if scheme not in ("http", "https") or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        return None
    default_port = 443 if scheme == "https" else 80
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port is not None and port != default_port else host
    path = parsed.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def _validate_personal_parser_binding(request: Request, c: Container) -> None:
    """校对个人 X-Parser-Key 的目标 MinerU 地址绑定，防止把官方个人 Token 误发往被管理员更改的第三方地址。"""
    personal_token = (request.headers.get("x-parser-key") or "").strip()
    if not personal_token:
        return
    snap = c.runtime_config.snapshot() if c.runtime_config is not None else None
    active_raw = snap.data.parser.mineru_base_url if snap is not None else _DEFAULT_MINERU_URL
    active_norm = _normalize_parser_base_url(active_raw) or active_raw.strip().rstrip("/")
    default_norm = _normalize_parser_base_url(_DEFAULT_MINERU_URL) or _DEFAULT_MINERU_URL
    header_raw = (request.headers.get("x-parser-base-url") or "").strip()
    if header_raw:
        header_norm = _normalize_parser_base_url(header_raw)
        if header_norm is None or header_norm != active_norm:
            raise HTTPException(
                status_code=400,
                detail="MinerU 服务地址已变更，请刷新页面并确认后再使用个人 Token",
            )
    elif active_norm != default_norm:
        raise HTTPException(
            status_code=400,
            detail="当前站点使用了自定义 MinerU 服务地址，请刷新页面并确认目标地址后再使用个人 Token",
        )


def container(request: Request) -> Container:
    return request.app.state.container


def pipeline(request: Request) -> Pipeline:
    return container(request).pipeline


def validate_user_llm_url(value: str, *, allow_private: bool = False) -> str:
    """限制浏览器传入的 LLM 地址，避免公开部署被用作内网请求代理。"""
    try:
        return validate_llm_url(value, allow_private=allow_private, require_https=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_protocol_header(raw: str | None) -> Literal["openai", "anthropic"] | None:
    if raw is None or raw == "":
        return None
    if raw not in ("openai", "anthropic"):
        raise HTTPException(status_code=400, detail="不支持的翻译协议")
    return raw


def _byok_secrets_from_request(request: Request, *, check_parser_binding: bool = True) -> Secrets:
    c = container(request)
    if check_parser_binding:
        _validate_personal_parser_binding(request, c)
    headers = request.headers
    x_llm_base_url = headers.get("x-llm-base-url")
    if x_llm_base_url is not None and x_llm_base_url != "":
        x_llm_base_url = validate_user_llm_url(x_llm_base_url, allow_private=c.settings.allow_private_llm_urls)
    else:
        x_llm_base_url = None
    return c.secrets(
        base_url=x_llm_base_url,
        api_key=headers.get("x-llm-key"),
        model=headers.get("x-llm-model"),
        parser_token=headers.get("x-parser-key"),
        paddle_token=headers.get("x-paddle-key"),
        protocol=_parse_protocol_header(headers.get("x-llm-protocol")),
    )


def secrets(
    request: Request,
    x_llm_base_url: str | None = Header(default=None),
    x_llm_key: str | None = Header(default=None),
    x_llm_model: str | None = Header(default=None),
    x_parser_key: str | None = Header(default=None),
    x_paddle_key: str | None = Header(default=None),
    x_llm_protocol: Literal["openai", "anthropic"] | None = Header(default=None),
    x_translation_mode: str | None = Header(default=None),
) -> Secrets:
    """仅供自备 API Key 路径（/api/llm/check、/api/llm/models、/api/glossary/parse）使用，严禁借免费头白嫖站点 Key。"""
    if x_translation_mode is not None and x_translation_mode.strip().lower() == "free":
        raise HTTPException(status_code=400, detail="此接口仅支持自备 API Key，不支持免费体验通道")
    if x_llm_base_url is not None:
        x_llm_base_url = validate_user_llm_url(
            x_llm_base_url, allow_private=container(request).settings.allow_private_llm_urls
        )
    return container(request).secrets(
        base_url=x_llm_base_url,
        api_key=x_llm_key,
        model=x_llm_model,
        parser_token=x_parser_key,
        paddle_token=x_paddle_key,
        protocol=x_llm_protocol,
    )


def resolve_upload_secrets(
    request: Request,
    x_translation_mode: str | None = Header(default=None),
) -> Secrets:
    """解析上传任务的凭据：X-Translation-Mode: free 完全走站点热配置快照，忽略浏览器所有 X-LLM-*/X-Parser-* 头。"""
    mode = (x_translation_mode or "byok").strip().lower()
    if mode == "free":
        return container(request).free_secrets(require_parser=True, parser=None)
    if mode != "byok":
        raise HTTPException(status_code=400, detail="不支持的翻译通道模式")
    return _byok_secrets_from_request(request)


def resolve_existing_job_secrets(request: Request, job: Job, *, for_retranslate: bool = False) -> Secrets:
    """按任务已落库的 channel_mode 解析重试/重翻凭据：免费任务绝不读取浏览器 X-LLM-*/X-Parser-* 头。

    已完成解析的任务重试与重翻不再强求解析 Token，只要免费 LLM 可用即可继续；需解析时仅按任务所选解析器校验。
    """
    c = container(request)
    needs_parser = (
        not for_retranslate
        and job.stage == "parse"
        and not c.pipeline.deps.artifacts.path(job.id, "layout.json").exists()
    )
    if job.channel_mode == "free":
        return c.free_secrets(
            job.channel_provider_id or None,
            require_parser=needs_parser,
            parser=job.options.parser if needs_parser else None,
        )
    return _byok_secrets_from_request(
        request,
        check_parser_binding=needs_parser and job.options.parser == "mineru-cloud",
    )
