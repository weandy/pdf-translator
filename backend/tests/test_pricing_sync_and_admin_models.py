"""后台模型拉取、GitHub 模型单价同步（含 24h 自动调度）与在途任务完整展示的离线集成测试。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import _sync_pricing_periodically, create_app
from pdftr.config import Settings
from pdftr.container import Container, build_container
from pdftr.domain.admin import hash_password
from pdftr.domain.job import Job, JobStatus
from pdftr.domain.llm import ChatError, ChatUsage
from pdftr.domain.runtime_config import DEFAULT_PRICING_SOURCE_URL
from pdftr.pipeline.pricing_sync import (
    PriceFetchResult,
    PricingSyncService,
    parse_remote_price_payload,
)
from pdftr.translation.cost import cost_cny

PASSWORD = "synthetic-admin-pass-pricing-2026"
ORIGIN = "https://admin.test"


def _gen_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _fake_addrinfo(ip: str = "93.184.216.34"):
    def _resolver(host: str, port: object, *args: object, **kwargs: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    return _resolver


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture(autouse=True)
def mock_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo())


def _make_system(tmp_path: Path, password_hash: str) -> tuple[TestClient, Container]:
    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            admin_password_hash=password_hash,
            admin_origin=ORIGIN,
            trusted_proxy_cidrs=("127.0.0.1/32",),
            config_secret_key=_gen_key(),
        )
    )
    return TestClient(create_app(c), base_url=ORIGIN), c


def _login_headers(client: TestClient) -> dict[str, str]:
    resp = client.post("/api/admin/session", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert resp.status_code == 200, resp.text
    return {"Origin": ORIGIN, "X-CSRF-Token": resp.json()["csrf_token"]}


def test_admin_path_reserves_tos() -> None:
    with pytest.raises(ValueError, match="未占用的单一路径段"):
        Settings(_env_file=None, admin_path="/tos")  # type: ignore[call-arg]


def test_parse_remote_price_formats_litellm_models_dev_and_pdftr() -> None:
    # 1. Wei-Shaw / LiteLLM 格式（USD / token），自动过滤 sample_spec 与 embedding
    litellm_payload = {
        "sample_spec": {"input_cost_per_token": 0.001, "output_cost_per_token": 0.002},
        "gpt-4o": {
            "mode": "chat",
            "input_cost_per_token": 2.5e-6,
            "output_cost_per_token": 1e-5,
            "cache_read_input_token_cost": 1.25e-6,
        },
        "deepseek-chat": {
            "mode": "chat",
            "input_cost_per_token": 2.7e-7,
            "output_cost_per_token": 1.1e-6,
            "input_cost_per_token_cache_hit": 7e-8,
        },
        "text-embedding-3-large": {
            "mode": "embedding",
            "input_cost_per_token": 1.3e-7,
            "output_cost_per_token": 0.0,
        },
    }
    entries, fmt = parse_remote_price_payload(json.dumps(litellm_payload).encode("utf-8"), source_format="auto")
    assert fmt == "litellm"
    assert set(entries.keys()) == {"gpt-4o", "deepseek-chat"}
    gpt4o_cny = entries["gpt-4o"].to_cny_price(7.2)
    assert gpt4o_cny.cache_miss == pytest.approx(2.5 * 7.2)
    assert gpt4o_cny.output == pytest.approx(10.0 * 7.2)
    assert gpt4o_cny.cache_hit == pytest.approx(1.25 * 7.2)

    # 2. models.dev 格式（USD / 1M token）
    models_dev_payload = {
        "anthropic": {
            "models": {
                "claude-sonnet-4-5": {"cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3}},
            }
        }
    }
    md_entries, md_fmt = parse_remote_price_payload(
        json.dumps(models_dev_payload).encode("utf-8"), source_format="auto"
    )
    assert md_fmt == "models_dev"
    assert "claude-sonnet-4-5" in md_entries
    assert "anthropic/claude-sonnet-4-5" in md_entries
    assert md_entries["claude-sonnet-4-5"].to_cny_price(7.0).output == pytest.approx(105.0)

    # 3. pdftr 原生格式（CNY / 1M token）
    pdftr_payload = {
        "models": [{"model": "local-cny", "cache_hit": 0.1, "cache_miss": 1.0, "output": 2.0, "peak_factor": 1.5}]
    }
    pdftr_entries, pdftr_fmt = parse_remote_price_payload(
        json.dumps(pdftr_payload).encode("utf-8"), source_format="auto"
    )
    assert pdftr_fmt == "pdftr"
    assert pdftr_entries["local-cny"].to_cny_price(9.9).output == pytest.approx(2.0)
    assert pdftr_entries["local-cny"].to_cny_price(9.9).peak_factor == pytest.approx(1.5)


def test_overview_active_jobs_include_filename_and_created_at(tmp_path: Path, password_hash: str) -> None:
    client, c = _make_system(tmp_path, password_hash)
    try:
        with client:
            headers = _login_headers(client)
            del headers
            job = Job(
                id="active-job-full-001",
                filename="synthetic_transformer_survey.pdf",
                page_count=18,
                status=JobStatus.RUNNING,
                stage="translate",
                progress=0.62,
                model="deepseek-flash",
                channel_mode="free",
            )
            c.pipeline.deps.store.create_job(job)
            # 模拟一条正在运行的在途任务句柄
            c.pipeline._tasks[job.id] = object()  # type: ignore[assignment]
            try:
                resp = client.get("/api/admin/overview")
                assert resp.status_code == 200, resp.text
                active = resp.json()["active_jobs"]
                assert len(active) == 1
                assert active[0]["job_id"] == "active-job-full-001"
                assert active[0]["filename"] == "synthetic_transformer_survey.pdf"
                assert active[0]["created_at"]
            finally:
                c.pipeline._tasks.pop(job.id, None)
    finally:
        client.close()
        c.close()


def test_admin_list_models_free_provider_and_default_llm(
    tmp_path: Path, password_hash: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, c = _make_system(tmp_path, password_hash)
    try:
        with client:
            headers = _login_headers(client)
            assert c.runtime_config is not None
            raw_provider_key = "nvapi-stored-free-secret-xyz123"
            r_save = client.put(
                "/api/admin/config/free_translation",
                json={
                    "expected_revision": 0,
                    "enabled": False,
                    "providers": [
                        {
                            "id": "nim-1",
                            "name": "NVIDIA NIM",
                            "enabled": True,
                            "protocol": "openai",
                            "base_url": "https://integrate.api.nvidia.com/v1",
                            "model": "nvidia/nemotron-3-ultra-550b-a55b",
                            "api_key": {"action": "replace", "value": raw_provider_key},
                        }
                    ],
                },
                headers=headers,
            )
            assert r_save.status_code == 200, r_save.text

            seen_calls: list[tuple[str, str, str]] = []

            async def fake_lister(protocol: str, base_url: str, api_key: str) -> list[str]:
                seen_calls.append((protocol, base_url, api_key))
                if not api_key:
                    raise ChatError("需要 API Key", status_code=401)
                return ["model-b", "model-a"]

            c.runtime_config._model_lister = fake_lister

            # 1. 免费渠道沿用后台已加密存库 Key 拉取模型，绝不回显明文
            r_keep = client.post(
                "/api/admin/config/models",
                json={
                    "target": "free_provider",
                    "provider_id": "nim-1",
                    "protocol": "openai",
                    "base_url": "https://integrate.api.nvidia.com/v1",
                    "api_key": {"action": "keep"},
                },
                headers=headers,
            )
            assert r_keep.status_code == 200, r_keep.text
            assert r_keep.json()["ok"] is True
            assert r_keep.json()["models"] == ["model-b", "model-a"]
            assert raw_provider_key not in r_keep.text
            assert seen_calls[-1] == ("openai", "https://integrate.api.nvidia.com/v1", raw_provider_key)

            # 2. 修改地址时 keep 旧 Key 必须被 400 拒绝（防止密钥发往新地址）
            r_conflict = client.post(
                "/api/admin/config/models",
                json={
                    "target": "free_provider",
                    "provider_id": "nim-1",
                    "protocol": "openai",
                    "base_url": "https://other-host.example.com/v1",
                    "api_key": {"action": "keep"},
                },
                headers=headers,
            )
            assert r_conflict.status_code == 400
            assert "更换或禁用原密钥" in r_conflict.json()["detail"]

            # 3. 表单未保存新 Key（replace）可直接拉取且不改库中旧 Key
            temp_new_key = "sk-unsaved-form-key-9988"
            r_replace = client.post(
                "/api/admin/config/models",
                json={
                    "target": "free_provider",
                    "provider_id": "nim-1",
                    "protocol": "openai",
                    "base_url": "https://other-host.example.com/v1",
                    "api_key": {"action": "replace", "value": temp_new_key},
                },
                headers=headers,
            )
            assert r_replace.status_code == 200
            assert r_replace.json()["ok"] is True
            assert temp_new_key not in r_replace.text
            assert seen_calls[-1] == ("openai", "https://other-host.example.com/v1", temp_new_key)

            # 4. 自备预设：先无 Key 试拉收到 401 返回 requires_api_key=True，再传一次性 probe_api_key 成功且不落库
            r_pub_401 = client.post(
                "/api/admin/config/models",
                json={
                    "target": "default_llm",
                    "protocol": "openai",
                    "base_url": "https://api.deepseek.com",
                },
                headers=headers,
            )
            assert r_pub_401.status_code == 200
            assert r_pub_401.json()["ok"] is False
            assert r_pub_401.json()["requires_api_key"] is True

            probe_secret = "sk-one-time-probe-secret-777"
            r_probe = client.post(
                "/api/admin/config/models",
                json={
                    "target": "default_llm",
                    "protocol": "openai",
                    "base_url": "https://api.deepseek.com",
                    "probe_api_key": probe_secret,
                },
                headers=headers,
            )
            assert r_probe.status_code == 200
            assert r_probe.json()["ok"] is True
            assert probe_secret not in r_probe.text
            # 数据库中绝无 probe_secret
            with sqlite3.connect(c.settings.data_dir / "app.db") as conn:
                rows = conn.execute("SELECT config_json, secrets_json FROM runtime_config").fetchall()
                assert all(probe_secret not in str(row) for row in rows)

            # 5. 真实公开 /models（无鉴权 MockTransport）与私网 SSRF 拦截
            c.runtime_config._model_lister = None
            real_client_cls = httpx2.AsyncClient
            mock_transport = httpx2.MockTransport(
                lambda req: httpx2.Response(
                    200,
                    json={"data": [{"id": "nvidia/nemotron-3-ultra-550b-a55b"}, {"id": "meta/llama-3.1-70b"}]},
                )
            )
            monkeypatch.setattr(
                httpx2,
                "AsyncClient",
                lambda *a, **kw: real_client_cls(
                    **{**kw, "transport": mock_transport, "event_hooks": None},
                ),
            )
            r_pub_ok = client.post(
                "/api/admin/config/models",
                json={
                    "target": "default_llm",
                    "protocol": "openai",
                    "base_url": "https://integrate.api.nvidia.com/v1",
                },
                headers=headers,
            )
            assert r_pub_ok.status_code == 200
            assert r_pub_ok.json()["ok"] is True
            assert r_pub_ok.json()["models"] == ["meta/llama-3.1-70b", "nvidia/nemotron-3-ultra-550b-a55b"]

            r_ssrf = client.post(
                "/api/admin/config/models",
                json={
                    "target": "default_llm",
                    "protocol": "openai",
                    "base_url": "http://127.0.0.1:8000/v1",
                },
                headers=headers,
            )
            assert r_ssrf.status_code == 400
    finally:
        client.close()
        c.close()


def test_pricing_sync_dual_layer_priority_rate_recalc_etag_and_24h_schedule(tmp_path: Path, password_hash: str) -> None:
    client, c = _make_system(tmp_path, password_hash)
    try:
        with client:
            headers = _login_headers(client)
            assert c.runtime_config is not None

            # 默认配置即指向用户指定的 Wei-Shaw 源且 24h 自动同步开启
            cfg0 = client.get("/api/admin/config").json()
            assert cfg0["pricing"]["sync"]["source_url"] == DEFAULT_PRICING_SOURCE_URL
            assert cfg0["pricing"]["sync"]["auto_sync_enabled"] is True
            assert cfg0["pricing"]["sync"]["sync_interval_hours"] == 24
            assert cfg0["pricing_sync_status"]["status"] == "never"

            remote_json = json.dumps(
                {
                    "gpt-4o": {
                        "mode": "chat",
                        "input_cost_per_token": 2.5e-6,
                        "output_cost_per_token": 1e-5,
                        "cache_read_input_token_cost": 1.25e-6,
                    },
                    "deepseek-flash": {
                        "mode": "chat",
                        "input_cost_per_token": 3e-7,
                        "output_cost_per_token": 1.2e-6,
                        "cache_read_input_token_cost": 6e-9,
                    },
                }
            ).encode("utf-8")

            fetch_calls: list[tuple[str, str]] = []
            mode = {"state": "ok"}

            async def fake_fetcher(url: str, etag: str) -> PriceFetchResult:
                fetch_calls.append((url, etag))
                if mode["state"] == "fail":
                    raise ValueError("模拟上游 503")
                if mode["state"] == "304" and etag == '"etag-v1"':
                    return PriceFetchResult(status_code=304, etag='"etag-v1"', not_modified=True)
                return PriceFetchResult(status_code=200, body=remote_json, etag='"etag-v1"', not_modified=False)

            c.runtime_config._pricing_sync = PricingSyncService(
                tmp_path / "pricing" / "remote_prices.json",
                allow_private=False,
                fetcher=fake_fetcher,
            )

            # 1. 手动触发同步：gpt-4o 与 openai/gpt-4o 均可命中计价；
            #    未自定义热表时 deepseek-flash 保留内置官网人民币价(output=4.0, peak=2.0)
            r_sync = client.post("/api/admin/pricing/sync", json={}, headers=headers)
            assert r_sync.status_code == 200, r_sync.text
            sync_body = r_sync.json()
            assert sync_body["status"] == "ok"
            assert sync_body["model_count"] == 2
            assert sync_body["synced_at"] is not None

            snap = c.runtime_config.snapshot()
            assert snap.price_overrides is not None
            usage = ChatUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
            # gpt-4o 命中远程价格，且带 openai/ 前缀同样命中
            expected_gpt4o = (2.5 + 10.0) * 7.25
            assert cost_cny("gpt-4o", usage, peak=True, prices=snap.price_overrides) == pytest.approx(expected_gpt4o)
            assert cost_cny("openai/gpt-4o", usage, peak=False, prices=snap.price_overrides) == pytest.approx(
                expected_gpt4o
            )
            # deepseek-flash 保持内置官网人民币价与高峰倍率 2.0
            assert cost_cny("deepseek-flash", usage, peak=True, prices=snap.price_overrides) == pytest.approx(
                (1.0 + 4.0) * 2.0
            )
            est_gpt4o = client.get("/api/estimate", params={"pages": 10, "model": "gpt-4o"}).json()
            assert est_gpt4o["cny_low"] is not None and est_gpt4o["cny_low"] > 0

            # 2. 手动热表优先于远程同步缓存，且调整 usd_to_cny=8.0 立即重算远程缓存
            r_put = client.put(
                "/api/admin/config/pricing",
                json={
                    "expected_revision": 0,
                    "models": [
                        {
                            "model": "gpt-4o",
                            "cache_hit": 1.0,
                            "cache_miss": 2.0,
                            "output": 3.0,
                            "peak_factor": 1.0,
                        }
                    ],
                    "sync": {
                        "source_url": DEFAULT_PRICING_SOURCE_URL,
                        "source_format": "auto",
                        "usd_to_cny": 8.0,
                        "auto_sync_enabled": True,
                        "sync_interval_hours": 24,
                    },
                },
                headers=headers,
            )
            assert r_put.status_code == 200, r_put.text
            snap2 = c.runtime_config.snapshot()
            assert snap2.price_overrides is not None
            # 手动热表覆盖 gpt-4o（2 + 3 = 5.0）
            assert cost_cny("gpt-4o", usage, peak=False, prices=snap2.price_overrides) == pytest.approx(5.0)
            # 未在手动热表中的 deepseek-flash 落到远程缓存并实时按新汇率 8.0 换算
            assert cost_cny("deepseek-flash", usage, peak=False, prices=snap2.price_overrides) == pytest.approx(
                (0.3 + 1.2) * 8.0
            )

            # 3. 远程同步失败时保留已有本地缓存，绝不破坏线上计价
            mode["state"] = "fail"
            r_fail = client.post("/api/admin/pricing/sync", json={}, headers=headers)
            assert r_fail.status_code == 200
            assert r_fail.json()["status"] == "error"
            assert r_fail.json()["model_count"] == 2
            assert "503" in r_fail.json()["last_error"]
            assert cost_cny(
                "deepseek-flash", usage, peak=False, prices=c.runtime_config.snapshot().price_overrides
            ) == pytest.approx((0.3 + 1.2) * 8.0)

            # 4. 24h 自动调度：刚同步过不到 24h 时跳过；把 synced_at 拨回 25 小时前则按 ETag 304 自动续期
            mode["state"] = "304"
            calls_before = len(fetch_calls)
            assert asyncio.run(c.runtime_config.sync_pricing_if_due()) is False
            assert len(fetch_calls) == calls_before

            c.runtime_config._pricing_sync._synced_at = datetime.now(UTC) - timedelta(hours=25)
            stop = asyncio.Event()

            async def run_scheduler_once() -> None:
                task = asyncio.create_task(
                    _sync_pricing_periodically(c, initial_delay_s=0.0, check_interval_s=3600.0, stop_event=stop)
                )
                await asyncio.sleep(0.05)
                stop.set()
                await task

            asyncio.run(run_scheduler_once())
            assert len(fetch_calls) == calls_before + 1
            assert fetch_calls[-1][1] == '"etag-v1"'
            assert c.runtime_config.admin_view().pricing_sync_status.status == "ok"

            # 5. 保存私网价格源 URL 必须被 400 SSRF 拦截
            r_bad_url = client.put(
                "/api/admin/config/pricing",
                json={
                    "expected_revision": 1,
                    "models": [],
                    "sync": {
                        "source_url": "http://127.0.0.1/prices.json",
                        "source_format": "auto",
                        "usd_to_cny": 7.25,
                        "auto_sync_enabled": True,
                        "sync_interval_hours": 24,
                    },
                },
                headers=headers,
            )
            assert r_bad_url.status_code == 400
    finally:
        client.close()
        c.close()
