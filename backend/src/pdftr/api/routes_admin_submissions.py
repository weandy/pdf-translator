"""管理员提交明细：先验证会话和请求来源，再执行受限查询。"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from pdftr.api.admin_deps import enforce_csrf, enforce_query_rate, require_admin
from pdftr.api.client_ip import resolve_client_ip
from pdftr.domain.submissions import IPSource, SubmissionPage, SubmissionSearch

router = APIRouter(prefix="/api/admin/submissions", tags=["admin"])


class AdminClientOut(BaseModel):
    submitter_ip: str | None = None
    ip_source: IPSource = "unknown"
    notice_version: str | None = None


@router.get("/client", response_model=AdminClientOut)
def current_client(request: Request) -> AdminClientOut:
    """仅管理员核验本次连接的可信地址，不记录额外访问或提交。"""
    identity = require_admin(request)
    enforce_query_rate(request, identity)
    client = resolve_client_ip(request, request.app.state.container.settings)
    return AdminClientOut(submitter_ip=client.address, ip_source=client.source)


@router.post("/search", response_model=SubmissionPage)
def search_submissions(body: SubmissionSearch, request: Request) -> SubmissionPage:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    enforce_query_rate(request, identity)
    return request.app.state.container.pipeline.deps.store.search_submissions(body)
