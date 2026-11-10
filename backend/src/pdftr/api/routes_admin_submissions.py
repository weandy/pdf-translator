"""管理员提交明细：先验证会话和请求来源，再执行受限查询。"""

from fastapi import APIRouter, Request

from pdftr.api.admin_deps import enforce_csrf, enforce_query_rate, require_admin
from pdftr.domain.submissions import SubmissionPage, SubmissionSearch

router = APIRouter(prefix="/api/admin/submissions", tags=["admin"])


@router.post("/search", response_model=SubmissionPage)
def search_submissions(body: SubmissionSearch, request: Request) -> SubmissionPage:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    enforce_query_rate(request, identity)
    return request.app.state.container.pipeline.deps.store.search_submissions(body)
