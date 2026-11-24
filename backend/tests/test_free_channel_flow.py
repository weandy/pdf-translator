"""免费通道端到端流、凭据隔离、故障恢复重试、维护模式、DynamicSlots 热调限与审计筛选集成测试。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
import time
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.admin import hash_password
from pdftr.domain.layout import LayoutResult
from pdftr.domain.llm import ChatError, ChatOptions, ChatResult, ChatUsage
from pdftr.parsing.mineru_normalize import normalize
from pdftr.pipeline.runner import DynamicSlots
from pdftr.pipeline.secrets import Secrets

PASSWORD = "synthetic-free-flow-admin-pass"
ORIGIN = "https://admin.test"
FIXTURES = Path(__file__).parent / "fixtures"
TEXTS = [
    (0, 52, 56, "A Synthetic Title"),
    (0, 52, 609, "Let"),
    (0, 124, 609, "f(x)"),
    (0, 154, 609, "be a function that"),
    (0, 312, 109, "continues in the next column"),
    (0, 52, 556, "Figure 1: A caption."),
    (0, 312, 712, "[1] Some reference."),
    (0, 52, 18, "Running header"),
    (0, 52, 755, "1 A footnote."),
    (1, 52, 69, "and ends on the next page."),
    (1, 52, 109, "A normal paragraph."),
]


def _make_pdf() -> bytes:
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=600, height=800)
    for page, x, y, text in TEXTS:
        doc[page].insert_text((x, y), text, fontsize=9, fontname="tiro")
    return doc.tobytes()


class _FakeParser:
    name = "fake"
    is_cloud = True

    def __init__(self, seen_tokens: list[str], token: str) -> None:
        self._seen_tokens = seen_tokens
        self._token = token

    async def parse(self, pdf: Path, pages=None, raw_dir=None, on_progress=None) -> LayoutResult:
        self._seen_tokens.append(self._token)
        return normalize(json.loads((FIXTURES / "mineru_layout_min.json").read_text(encoding="utf-8")))


class _FakeChat:
    def __init__(self, sec: Secrets, seen_secrets: list[Secrets], fail_flag: dict[str, bool]) -> None:
        self.model = sec.llm_model
        self._sec = sec
        self._seen = seen_secrets
        self._fail = fail_flag

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        self._seen.append(self._sec)
        if self._fail["chat"]:
            raise ChatError("模拟免费上游临时故障", retryable=False)
        if "抽取需要全文统一译法的专业术语" in user:
            return ChatResult(text="领域：测试\nfunction=函数", usage=ChatUsage(prompt_tokens=60, completion_tokens=20))
        out = []
        for line in user.splitlines():
            if m := re.match(r"^(\d+)\|(.*)$", line):
                text = re.sub(r"<(m\d+)>.*?</\1>", r"<\1/>", m.group(2))
                if "be a function that" in text:
                    suffix = "并在下一页结束。" if "ends on the next page" in text else "。"
                    text = "设 <m1/> 为 function，跨栏续接" + suffix
                out.append(f"{m.group(1)}|译:{text}")
        return ChatResult(
            text="\n".join(out),
            usage=ChatUsage(prompt_tokens=80, completion_tokens=30),
            used_fallback=self._fail.get("fallback", False),
            actual_model="fallback-model" if self._fail.get("fallback") else self.model,
        )

    async def list_models(self) -> list[str]:
        return [self.model]


def _wait(client: TestClient, job_id: str) -> dict:
    for _ in range(120):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("任务超时")


@pytest.fixture(autouse=True)
def mock_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolver(host: str, port: object, *args: object, **kwargs: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolver)


def _setup_free_env(
    tmp_path: Path,
    secret_key: str,
    password_hash: str,
    seen_secrets: list[Secrets],
    seen_tokens: list[str],
    fail_flag: dict[str, bool],
):
    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            font_dir=tmp_path / "fonts",
            admin_password_hash=password_hash,
            admin_origin=ORIGIN,
            trusted_proxy_cidrs=("127.0.0.1/32",),
            submission_audit_enabled=True,
            config_secret_key=secret_key,
            mineru_api_token="site-mineru-token-001",
        ),
        deps_override={
            "parser_factory": lambda sec, name: _FakeParser(seen_tokens, sec.token_for(name)),
            "chat_factory": lambda sec: _FakeChat(sec, seen_secrets, fail_flag),
        },
    )
    return c


def _enable_free_channel(client: TestClient) -> dict[str, str]:
    login = client.post("/api/admin/session", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert login.status_code == 200
    headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
    r = client.put(
        "/api/admin/config/free_translation",
        json={
            "expected_revision": 0,
            "enabled": True,
            "max_pages": 20,
            "notice": "仅限公开论文",
            "providers": [
                {
                    "id": "free-nim",
                    "name": "NIM 免费线",
                    "enabled": True,
                    "protocol": "openai",
                    "base_url": "https://integrate.api.nvidia.com/v1",
                    "model": "nvidia/nemotron-3-super-120b-a12b",
                    "fallback_model": "nvidia/nemotron-3-nano",
                    "timeout_s": 65,
                    "api_key": {"action": "replace", "value": "nvapi-site-free-key-777"},
                }
            ],
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return headers


def test_free_upload_ignores_spoofed_headers_and_recovers_on_retry_and_retranslate(tmp_path: Path) -> None:
    secret_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    pw_hash = hash_password(PASSWORD)
    seen_secrets: list[Secrets] = []
    seen_tokens: list[str] = []
    fail_flag = {"chat": False, "fallback": True}

    c = _setup_free_env(tmp_path, secret_key, pw_hash, seen_secrets, seen_tokens, fail_flag)
    try:
        with TestClient(create_app(c), base_url=ORIGIN, client=("127.0.0.1", 50001)) as client:
            admin_headers = _enable_free_channel(client)

            # 1. /api/llm/check 与 /api/llm/models 拒绝通过 X-Translation-Mode: free 白嫖站点 Key
            assert client.post("/api/llm/check", headers={"X-Translation-Mode": "free"}).status_code == 400
            assert client.get("/api/llm/models", headers={"X-Translation-Mode": "free"}).status_code == 400

            # 2. 免费上传：故意伪造 X-LLM-* 头，验证服务端彻底忽略并使用站点免费 Key
            spoofed_headers = {
                "X-Translation-Mode": "free",
                "X-LLM-Base-Url": "https://spoofed.example.com/v1",
                "X-LLM-Key": "sk-spoofed-browser-key",
                "X-LLM-Model": "spoofed-model",
                "X-Parser-Key": "spoofed-parser-token",
                "X-Forwarded-For": "198.51.100.42",
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
                ),
            }
            fail_flag["chat"] = True
            upload = client.post(
                "/api/jobs",
                files={"file": ("free_paper.pdf", _make_pdf(), "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers=spoofed_headers,
            )
            assert upload.status_code == 202, upload.text
            job_id = upload.json()["id"]
            assert upload.json()["channel_mode"] == "free"
            failed_job = _wait(client, job_id)
            assert failed_job["status"] == "failed"
            assert seen_tokens[-1] == "site-mineru-token-001"
            assert seen_secrets[-1].llm_api_key == "nvapi-site-free-key-777"
            assert seen_secrets[-1].llm_base_url == "https://integrate.api.nvidia.com/v1"
            assert seen_secrets[-1].channel_mode == "free"

            # 3. 失败后重试（不带任何 Key 头，甚至带伪造头）自动恢复免费快照完成翻译
            fail_flag["chat"] = False
            retry = client.post(
                f"/api/jobs/{job_id}/retry",
                headers={"X-LLM-Key": "sk-spoofed-retry", "X-LLM-Base-Url": "https://evil.example.com/v1"},
            )
            assert retry.status_code == 202
            done_job = _wait(client, job_id)
            assert done_job["status"] == "done", done_job
            assert done_job["channel_mode"] == "free"
            assert done_job["fallback_used"] is True
            assert seen_secrets[-1].llm_api_key == "nvapi-site-free-key-777"

            # 4. 单段重翻也走免费快照并忽略浏览器头
            doc = client.get(f"/api/jobs/{job_id}/document").json()
            seg_id = doc["segments"][0]["id"]
            seen_secrets.clear()
            retrans = client.post(
                f"/api/segments/{seg_id}/retranslate",
                json={"quality": "careful"},
                headers={"X-LLM-Key": "sk-spoofed-retranslate"},
            )
            assert retrans.status_code == 200, retrans.text
            assert seen_secrets[-1].llm_api_key == "nvapi-site-free-key-777"

            # 5. 提交一条自备 Key 任务用于审计组合筛选对照
            byok_upload = client.post(
                "/api/jobs",
                files={"file": ("byok_paper.pdf", _make_pdf(), "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers={
                    "X-LLM-Key": "sk-user-byok-key",
                    "X-Parser-Key": "user-mineru-token",
                    "X-LLM-Model": "deepseek-flash",
                    "X-Forwarded-For": "198.51.100.88",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
                },
            )
            assert byok_upload.status_code == 202
            byok_done = _wait(client, byok_upload.json()["id"])
            assert byok_done["status"] == "done" and byok_done["channel_mode"] == "byok"

            # 6. 验证审计表完整保存 IP + UA 解析，且 POST /api/admin/submissions/search 支持组合筛选
            search_free_mobile = client.post(
                "/api/admin/submissions/search",
                json={"channel_mode": "free", "status": "done", "client_device": "mobile"},
                headers=admin_headers,
            )
            assert search_free_mobile.status_code == 200
            items = search_free_mobile.json()["items"]
            assert len(items) == 1
            rec = items[0]
            assert rec["job_id"] == job_id
            assert rec["submitter_ip"] == "198.51.100.42"
            assert rec["channel_mode"] == "free"
            assert rec["client_device"] == "mobile"
            assert rec["client_os"] == "iOS"
            assert rec["client_browser"] == "Safari"
            assert rec["fallback_used"] is True

            search_byok_desktop = client.post(
                "/api/admin/submissions/search",
                json={"channel_mode": "byok", "status": "done", "client_device": "desktop"},
                headers=admin_headers,
            )
            assert search_byok_desktop.json()["total"] == 1

            # 7. 维护模式：拦截新上传、拦截重翻，但允许读取与导出已完成任务
            cfg_now = client.get("/api/admin/config").json()
            maint = client.put(
                "/api/admin/config/system",
                json={
                    **cfg_now["system"],
                    "expected_revision": cfg_now["revision"],
                    "maintenance_mode": True,
                    "site_notice": "正在维护升级，请稍候",
                },
                headers=admin_headers,
            )
            assert maint.status_code == 200
            blocked_up = client.post(
                "/api/jobs",
                files={"file": ("blocked.pdf", _make_pdf(), "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers={"X-Translation-Mode": "free"},
            )
            assert blocked_up.status_code == 503 and "正在维护升级" in blocked_up.json()["detail"]
            blocked_retrans = client.post(
                f"/api/segments/{seg_id}/retranslate",
                json={"quality": "default"},
            )
            assert blocked_retrans.status_code == 503
            # 已完成任务仍可阅读与导出
            assert client.get(f"/api/jobs/{job_id}").status_code == 200
            assert client.get(f"/api/jobs/{job_id}/document").status_code == 200
            assert client.get(f"/api/jobs/{job_id}/export", params={"mode": "mono"}).status_code == 200
    finally:
        if c.admin:
            c.admin.store.close()
        if c.runtime_config:
            c.runtime_config.close()
        c.pipeline.deps.store.close()  # type: ignore[attr-defined]
        c.pipeline.deps.cache.close()  # type: ignore[attr-defined]


def test_free_job_restart_recovery_still_uses_free_snapshot(tmp_path: Path) -> None:
    secret_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    pw_hash = hash_password(PASSWORD)
    seen_secrets: list[Secrets] = []
    seen_tokens: list[str] = []
    fail_flag = {"chat": False}

    # 第一轮启动：配置免费通道并创建一个中断中的免费任务（落盘为 running 状态）
    c1 = _setup_free_env(tmp_path, secret_key, pw_hash, seen_secrets, seen_tokens, fail_flag)
    try:
        with TestClient(create_app(c1), base_url=ORIGIN) as client1:
            _enable_free_channel(client1)
            fail_flag["chat"] = True
            up = client1.post(
                "/api/jobs",
                files={"file": ("restart.pdf", _make_pdf(), "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers={"X-Translation-Mode": "free"},
            )
            job_id = up.json()["id"]
            _wait(client1, job_id)
            # 模拟重启前仍处于 running 状态
            job = c1.pipeline.deps.store.get_job(job_id)
            assert job is not None
            from pdftr.domain.job import JobStatus

            job.status = JobStatus.RUNNING
            c1.pipeline.deps.store.save_job(job)
    finally:
        if c1.admin:
            c1.admin.store.close()
        if c1.runtime_config:
            c1.runtime_config.close()
        c1.pipeline.deps.store.close()  # type: ignore[attr-defined]
        c1.pipeline.deps.cache.close()  # type: ignore[attr-defined]

    # 第二轮启动（全新容器、空内存 SecretStore）：lifespan 自动 recover_interrupted，再重试免费任务成功！
    seen_secrets.clear()
    fail_flag["chat"] = False
    c2 = _setup_free_env(tmp_path, secret_key, pw_hash, seen_secrets, seen_tokens, fail_flag)
    try:
        with TestClient(create_app(c2), base_url=ORIGIN) as client2:
            recovered = client2.get(f"/api/jobs/{job_id}").json()
            assert recovered["status"] == "failed" and "服务重启" in recovered["error"]
            assert recovered["channel_mode"] == "free"
            retry = client2.post(f"/api/jobs/{job_id}/retry")
            assert retry.status_code == 202
            done = _wait(client2, job_id)
            assert done["status"] == "done", done
            assert seen_secrets[-1].llm_api_key == "nvapi-site-free-key-777"
    finally:
        if c2.admin:
            c2.admin.store.close()
        if c2.runtime_config:
            c2.runtime_config.close()
        c2.pipeline.deps.store.close()  # type: ignore[attr-defined]
        c2.pipeline.deps.cache.close()  # type: ignore[attr-defined]


def test_dynamic_slots_hot_increase_and_cancellation_no_leak() -> None:
    async def scenario() -> None:
        slots = DynamicSlots(1, lambda n: f"waiting {n}")
        entered: list[str] = []
        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def worker(name: str, gate: asyncio.Event) -> None:
            async with slots.hold():
                entered.append(name)
                await gate.wait()

        t1 = asyncio.create_task(worker("first", release_first))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(worker("second", release_second))
        t3 = asyncio.create_task(worker("third", release_second))
        await asyncio.sleep(0.02)
        assert entered == ["first"]
        assert slots.stats() == (1, 1, 2)

        # 取消等待中的 t3，不泄漏名额且等待队列缩短
        t3.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t3
        assert slots.stats() == (1, 1, 1)

        # 热升限 1 -> 2：立即唤醒 FIFO 队头的 second
        slots.set_limit(2)
        await asyncio.sleep(0.02)
        assert entered == ["first", "second"]
        assert slots.stats() == (2, 2, 0)

        # 持有期间取消 t1：确保释放名额
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        assert slots.stats() == (1, 2, 0)

        release_second.set()
        await t2
        assert slots.stats() == (0, 2, 0)

        # 降限 + 名额转交瞬间取消，计数完全恢复
        slots.set_limit(1)
        g2 = asyncio.Event()
        async with slots.hold():
            w2 = asyncio.create_task(worker("w2", g2))
            await asyncio.sleep(0.01)
            assert slots.stats() == (1, 1, 1)
        # 退出 hold() 瞬间名额已转交给 w2 的 Future，在 w2 获得调度前立刻取消 w2
        w2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w2
        assert slots.stats() == (0, 1, 0)

    asyncio.run(scenario())


def test_free_retranslate_shares_free_translation_slot_and_retry_enforces_queue_budget(tmp_path: Path) -> None:
    import concurrent.futures

    secret_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    pw_hash = hash_password(PASSWORD)
    seen_secrets: list[Secrets] = []
    seen_tokens: list[str] = []
    fail_flag = {"chat": False}

    active_calls = 0
    max_active_calls = 0
    delay_retranslate = {"enabled": False}

    class _CountingChat(_FakeChat):
        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            nonlocal active_calls, max_active_calls
            if delay_retranslate["enabled"]:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
                try:
                    await asyncio.sleep(0.15)
                    return await super().complete(system, user, opts)
                finally:
                    active_calls -= 1
            return await super().complete(system, user, opts)

    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            font_dir=tmp_path / "fonts",
            admin_password_hash=pw_hash,
            admin_origin=ORIGIN,
            trusted_proxy_cidrs=("127.0.0.1/32",),
            config_secret_key=secret_key,
            mineru_api_token="site-mineru-token-001",
            paddle_token="site-paddle-token-001",
        ),
        deps_override={
            "parser_factory": lambda sec, name: _FakeParser(seen_tokens, sec.token_for(name)),
            "chat_factory": lambda sec: _CountingChat(sec, seen_secrets, fail_flag),
        },
    )
    try:
        with TestClient(create_app(c), base_url=ORIGIN) as client:
            admin_headers = _enable_free_channel(client)

            # 创建并完成两个不同的免费任务
            job_ids: list[str] = []
            seg_ids: list[str] = []
            for idx in range(2):
                up = client.post(
                    "/api/jobs",
                    files={"file": (f"p{idx}.pdf", _make_pdf(), "application/pdf")},
                    data={"options": json.dumps({"cloud_consent": True})},
                    headers={"X-Translation-Mode": "free"},
                )
                assert up.status_code == 202
                jid = up.json()["id"]
                assert _wait(client, jid)["status"] == "done"
                doc = client.get(f"/api/jobs/{jid}/document").json()
                job_ids.append(jid)
                seg_ids.append(doc["segments"][0]["id"])

            # P0-05(1): free_max_concurrent_translations == 1 时两个免费任务并发重翻，最大在途模型调用数 == 1
            delay_retranslate["enabled"] = True

            def _call_retrans(sid: str) -> int:
                return client.post(f"/api/segments/{sid}/retranslate", json={"quality": "default"}).status_code

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                codes = list(pool.map(_call_retrans, seg_ids))
            assert codes == [200, 200]
            assert max_active_calls == 1
            delay_retranslate["enabled"] = False

            # P1-06: 停用 MinerU Token 后，已解析完成的免费任务仍可重翻与从 translate 阶段重试
            cfg_now = client.get("/api/admin/config").json()
            dis = client.put(
                "/api/admin/config/parser",
                json={
                    "expected_revision": cfg_now["revision"],
                    "mineru_base_url": "https://mineru.net",
                    "mineru_token": {"action": "disable"},
                },
                headers=admin_headers,
            )
            assert dis.status_code == 200
            r_retrans = client.post(f"/api/segments/{seg_ids[0]}/retranslate", json={"quality": "default"})
            assert r_retrans.status_code == 200, r_retrans.text

            # 模拟任务在 translate 阶段失败（layout.json 已存在），重试仍可成功
            from pdftr.domain.job import JobStatus

            j0 = c.pipeline.deps.store.get_job(job_ids[0])
            assert j0 is not None
            j0.status = JobStatus.FAILED
            j0.stage = "translate"
            c.pipeline.deps.store.save_job(j0)
            r_retry_ok = client.post(f"/api/jobs/{job_ids[0]}/retry")
            assert r_retry_ok.status_code == 202, r_retry_ok.text
            assert _wait(client, job_ids[0])["status"] == "done"

            # P0-05(2): 免费队列已满时，retry_job 返回 503 且原任务 status 保持 failed 不变
            j1 = c.pipeline.deps.store.get_job(job_ids[1])
            assert j1 is not None
            j1.status = JobStatus.FAILED
            j1.stage = "translate"
            c.pipeline.deps.store.save_job(j1)
            c.pipeline.receiving_free = 100
            try:
                r_busy = client.post(f"/api/jobs/{job_ids[1]}/retry")
                assert r_busy.status_code == 503
                after = c.pipeline.deps.store.get_job(job_ids[1])
                assert after is not None and after.status is JobStatus.FAILED
            finally:
                c.pipeline.receiving_free = 0

            # P1-10: paddle_public_enabled=False 时，即使站点配了 paddle_token 也拒绝无个人 X-Paddle-Key 的请求
            r_paddle_blocked = client.post(
                "/api/jobs",
                files={"file": ("paddle.pdf", _make_pdf(), "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True, "parser": "paddleocr-vl"})},
                headers={"X-LLM-Key": "sk-user-key"},
            )
            assert r_paddle_blocked.status_code == 400
            assert "PaddleOCR" in r_paddle_blocked.json()["detail"]

            # 带个人 X-Paddle-Key 允许使用
            r_paddle_personal = client.post(
                "/api/jobs",
                files={"file": ("paddle_ok.pdf", _make_pdf(), "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True, "parser": "paddleocr-vl"})},
                headers={"X-LLM-Key": "sk-user-key", "X-Paddle-Key": "personal-paddle-tok"},
            )
            assert r_paddle_personal.status_code == 202
            assert _wait(client, r_paddle_personal.json()["id"])["status"] == "done"

            # P1-11: 维护模式下 POST /api/jobs 在解析上传前直接 503，绝不创建任何临时文件
            cfg2 = client.get("/api/admin/config").json()
            assert (
                client.put(
                    "/api/admin/config/system",
                    json={
                        **cfg2["system"],
                        "expected_revision": cfg2["revision"],
                        "maintenance_mode": True,
                        "site_notice": "例行维护中",
                    },
                    headers=admin_headers,
                ).status_code
                == 200
            )
            before_files = set(c.pipeline.deps.artifacts.root.iterdir())
            r_maint = client.post(
                "/api/jobs",
                files={"file": ("huge.pdf", _make_pdf(), "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True})},
                headers={"X-LLM-Key": "sk-user-key", "X-Parser-Key": "p-tok"},
            )
            assert r_maint.status_code == 503 and "例行维护中" in r_maint.json()["detail"]
            assert set(c.pipeline.deps.artifacts.root.iterdir()) == before_files
    finally:
        c.close()


def test_free_channel_works_with_public_paddle_when_mineru_token_disabled(tmp_path: Path) -> None:
    secret_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    pw_hash = hash_password(PASSWORD)
    seen_secrets: list[Secrets] = []
    seen_tokens: list[str] = []
    seen_parsers: list[str] = []
    fail_flag = {"chat": False}

    def _parser_factory(sec: Secrets, name: str):
        seen_parsers.append(name)
        return _FakeParser(seen_tokens, sec.token_for(name))

    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            font_dir=tmp_path / "fonts",
            admin_password_hash=pw_hash,
            admin_origin=ORIGIN,
            trusted_proxy_cidrs=("127.0.0.1/32",),
            config_secret_key=secret_key,
            mineru_api_token="",
            paddle_token="site-paddle-only-tok",
        ),
        deps_override={
            "parser_factory": _parser_factory,
            "chat_factory": lambda sec: _FakeChat(sec, seen_secrets, fail_flag),
        },
    )
    try:
        with TestClient(create_app(c), base_url=ORIGIN) as client:
            admin_headers = _enable_free_channel(client)
            # 1. 仅配了 Paddle Token 但未公开 Paddle、且无 MinerU Token 时，免费入口尚未就绪
            assert client.get("/api/config").json()["free_channel"]["enabled"] is False
            assert client.get("/api/admin/overview").json()["services"]["free_ready"] is False

            # 2. 管理员开启公开 Paddle（并显式停用 MinerU Token）后，免费入口立刻就绪
            cfg = client.get("/api/admin/config").json()
            upd = client.put(
                "/api/admin/config/parser",
                json={
                    "expected_revision": cfg["revision"],
                    "mineru_base_url": "https://mineru.net",
                    "mineru_token": {"action": "disable"},
                    "paddle_public_enabled": True,
                },
                headers=admin_headers,
            )
            assert upd.status_code == 200
            pub = client.get("/api/config").json()
            assert pub["free_channel"]["enabled"] is True
            assert pub["paddle_public_enabled"] is True
            assert client.get("/api/admin/overview").json()["services"]["free_ready"] is True

            # 3. 免费用户选 mineru-cloud 被拒绝（MinerU 未就绪）
            r_mineru = client.post(
                "/api/jobs",
                files={"file": ("no_mineru.pdf", _make_pdf(), "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True, "parser": "mineru-cloud"})},
                headers={"X-Translation-Mode": "free"},
            )
            assert r_mineru.status_code == 400
            assert "MinerU" in r_mineru.json()["detail"]

            # 4. 免费用户选 paddleocr-vl 端到端跑通，完全不依赖 MinerU Token
            r_paddle = client.post(
                "/api/jobs",
                files={"file": ("free_paddle.pdf", _make_pdf(), "application/pdf")},
                data={"options": json.dumps({"cloud_consent": True, "parser": "paddleocr-vl"})},
                headers={"X-Translation-Mode": "free"},
            )
            assert r_paddle.status_code == 202, r_paddle.text
            done = _wait(client, r_paddle.json()["id"])
            assert done["status"] == "done"
            assert seen_parsers[-1] == "paddleocr-vl"
            assert seen_tokens[-1] == "site-paddle-only-tok"
    finally:
        c.close()
