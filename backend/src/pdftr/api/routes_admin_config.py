"""管理员总览与运行时配置热重载接口（全部受 AdminSecurityMiddleware 保护）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from pdftr.api.admin_deps import enforce_csrf, enforce_query_rate, require_admin
from pdftr.api.deps import container
from pdftr.domain.runtime_config import PricingSyncStatusView
from pdftr.pipeline.runtime_config import (
    ActiveJobSummaryOut,
    AdminConfigView,
    AdminListModelsIn,
    AdminListModelsOut,
    AdminOverviewOut,
    AdminQueuesOut,
    AdminServiceSummaryOut,
    ConfigRevisionsOut,
    ConnectionTestIn,
    ConnectionTestOut,
    DefaultLlmPatch,
    FreeTranslationPatch,
    GlossaryAdminView,
    GlossaryPatch,
    ParserPatch,
    PricingPatch,
    QueueSlotStats,
    RollbackIn,
    RuntimeConfigManager,
    SystemPatch,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _manager(request: Request) -> RuntimeConfigManager:
    mgr = container(request).runtime_config
    if mgr is None:
        raise HTTPException(500, "运行时配置服务未初始化")
    return mgr


@router.get("/overview", response_model=AdminOverviewOut)
def admin_overview(request: Request) -> AdminOverviewOut:
    identity = require_admin(request)
    enforce_query_rate(request, identity)
    c = container(request)
    mgr = _manager(request)
    snap = mgr.snapshot()
    q = c.pipeline.queue_snapshot()
    config_view = mgr.admin_view()
    stats = c.pipeline.deps.store.admin_overview_stats(days=7)
    active_jobs = [ActiveJobSummaryOut.model_validate(item) for item in c.pipeline.active_jobs_summary()]
    return AdminOverviewOut(
        queues=AdminQueuesOut(
            parse=QueueSlotStats(in_use=q["parse"][0], limit=q["parse"][1], waiting=q["parse"][2]),
            free=QueueSlotStats(in_use=q["free"][0], limit=q["free"][1], waiting=q["free"][2]),
            byok=QueueSlotStats(in_use=q["byok"][0], limit=q["byok"][1], waiting=q["byok"][2]),
            parse_reserved_for_byok=c.pipeline.parse_reserved_for_byok(),
        ),
        services=AdminServiceSummaryOut(
            free_enabled=snap.data.free_translation.enabled,
            free_ready=snap.free_channel_ready(),
            usable_providers=len(snap.active_free_providers()),
            mineru_configured=bool(snap.mineru_token.value) and not snap.mineru_token.corrupted,
            paddle_configured=bool(snap.paddle_token.value) and not snap.paddle_token.corrupted,
            glossary_terms=config_view.glossary.loaded_terms,
            glossary_reloading=config_view.glossary.reloading,
        ),
        warnings=list(snap.warnings),
        stats=stats,
        active_jobs=active_jobs,
        maintenance_mode=snap.data.system.maintenance_mode,
        revision=snap.revision,
        secret_key_ready=mgr.secret_key_ready,
    )


@router.get("/config", response_model=AdminConfigView)
def get_admin_config(request: Request) -> AdminConfigView:
    identity = require_admin(request)
    enforce_query_rate(request, identity)
    return _manager(request).admin_view()


@router.put("/config/free_translation", response_model=AdminConfigView)
def update_admin_config_free_translation(body: FreeTranslationPatch, request: Request) -> AdminConfigView:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return _manager(request).update_free_translation(body)


@router.put("/config/parser", response_model=AdminConfigView)
def update_admin_config_parser(body: ParserPatch, request: Request) -> AdminConfigView:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return _manager(request).update_parser(body)


@router.put("/config/default_llm", response_model=AdminConfigView)
def update_admin_config_default_llm(body: DefaultLlmPatch, request: Request) -> AdminConfigView:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return _manager(request).update_default_llm(body)


@router.put("/config/pricing", response_model=AdminConfigView)
def update_admin_config_pricing(body: PricingPatch, request: Request) -> AdminConfigView:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return _manager(request).update_pricing(body)


@router.put("/config/glossary", response_model=AdminConfigView)
def update_admin_config_glossary(body: GlossaryPatch, request: Request) -> AdminConfigView:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return _manager(request).update_glossary(body)


@router.put("/config/system", response_model=AdminConfigView)
def update_admin_config_system(body: SystemPatch, request: Request) -> AdminConfigView:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return _manager(request).update_system(body)


@router.post("/config/test", response_model=ConnectionTestOut)
async def test_admin_config(body: ConnectionTestIn, request: Request) -> ConnectionTestOut:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return await _manager(request).test_connection(body)


@router.post("/config/models", response_model=AdminListModelsOut)
async def list_admin_config_models(body: AdminListModelsIn, request: Request) -> AdminListModelsOut:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return await _manager(request).list_models(body)


@router.post("/pricing/sync", response_model=PricingSyncStatusView)
async def sync_admin_pricing(request: Request) -> PricingSyncStatusView:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return await _manager(request).sync_pricing(force=True)


@router.get("/config/revisions", response_model=ConfigRevisionsOut)
def list_config_revisions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConfigRevisionsOut:
    identity = require_admin(request)
    enforce_query_rate(request, identity)
    return _manager(request).list_revisions(limit=limit, offset=offset)


@router.post("/config/rollback", response_model=AdminConfigView)
def rollback_config(body: RollbackIn, request: Request) -> AdminConfigView:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return _manager(request).rollback(body)


@router.post("/glossary/reload", response_model=GlossaryAdminView)
def reload_glossary(request: Request) -> GlossaryAdminView:
    identity = require_admin(request)
    enforce_csrf(request, identity)
    return _manager(request).reload_glossary()
