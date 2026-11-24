"""管理后台运行时配置热重载、AES-256-GCM 加密边界、乐观锁、安全回滚、损坏降级与总览集成测试。"""

from __future__ import annotations

import base64
import os
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import Container, build_container
from pdftr.domain.admin import hash_password
from pdftr.pipeline.runtime_config import ConnectionTestOut

PASSWORD = "synthetic-runtime-admin-pass-2026"
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


def _make_system(
    tmp_path: Path,
    password_hash: str,
    *,
    config_secret_key: str = "",
    mineru_api_token: str = "",
) -> tuple[TestClient, Container]:
    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            admin_password_hash=password_hash,
            admin_origin=ORIGIN,
            trusted_proxy_cidrs=("127.0.0.1/32",),
            config_secret_key=config_secret_key,
            mineru_api_token=mineru_api_token,
        )
    )
    client = TestClient(create_app(c), base_url=ORIGIN)
    return client, c


def _close_system(client: TestClient, c: Container) -> None:
    client.close()
    if c.admin:
        c.admin.store.close()
    if c.runtime_config:
        c.runtime_config.close()
    c.pipeline.deps.store.close()  # type: ignore[attr-defined]
    c.pipeline.deps.cache.close()  # type: ignore[attr-defined]


def _login_headers(client: TestClient) -> dict[str, str]:
    resp = client.post("/api/admin/session", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert resp.status_code == 200, resp.text
    return {"Origin": ORIGIN, "X-CSRF-Token": resp.json()["csrf_token"]}


@pytest.fixture()
def authed_system(tmp_path: Path, password_hash: str) -> Iterator[tuple[TestClient, Container, dict[str, str], str]]:
    key = _gen_key()
    client, c = _make_system(tmp_path, password_hash, config_secret_key=key)
    with client:
        headers = _login_headers(client)
        try:
            yield client, c, headers, key
        finally:
            _close_system(client, c)


def test_empty_config_secret_key_auto_generates_and_persists_across_restart(tmp_path: Path, password_hash: str) -> None:
    key_file = tmp_path / ".config_secret.key"
    assert not key_file.exists()

    # 1. 不填 PDFTR_CONFIG_SECRET_KEY 时首次启动自动生成本地主密钥文件，并支持直接保存加密凭据
    client1, c1 = _make_system(tmp_path, password_hash, config_secret_key="")
    try:
        with client1:
            assert key_file.exists()
            raw_content = key_file.read_text(encoding="utf-8")
            headers = _login_headers(client1)
            cfg = client1.get("/api/admin/config").json()
            assert cfg["secret_key_ready"] is True
            assert cfg["warnings"] == []
            key_lock = next(item for item in cfg["env_locks"] if item["name"] == "PDFTR_CONFIG_SECRET_KEY")
            assert "自动生成" in key_lock["status"]

            saved = client1.put(
                "/api/admin/config/parser",
                json={
                    "expected_revision": 0,
                    "mineru_base_url": "https://mineru.net",
                    "mineru_token": {"action": "replace", "value": "auto-key-mineru-token-998877"},
                },
                headers=headers,
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["parser"]["mineru_token"]["configured"] is True
    finally:
        _close_system(client1, c1)

    # 2. 重启后复用同一本地主密钥文件，已加密保存的凭据完整解密无告警
    client2, c2 = _make_system(tmp_path, password_hash, config_secret_key="")
    try:
        with client2:
            assert key_file.read_text(encoding="utf-8") == raw_content
            assert c2.runtime_config is not None
            snap = c2.runtime_config.snapshot()
            assert snap.warnings == ()
            assert snap.mineru_token.value == "auto-key-mineru-token-998877"
    finally:
        _close_system(client2, c2)


def test_corrupted_local_secret_key_allows_non_secret_updates_but_blocks_secrets(
    tmp_path: Path, password_hash: str
) -> None:
    key_file = tmp_path / ".config_secret.key"
    key_file.write_text("not-a-valid-base64-key!!!\n", encoding="utf-8")

    client, c = _make_system(tmp_path, password_hash, config_secret_key="")
    try:
        with client:
            # 损坏文件绝不被静默覆盖重建
            assert key_file.read_text(encoding="utf-8") == "not-a-valid-base64-key!!!\n"
            headers = _login_headers(client)
            cfg = client.get("/api/admin/config").json()
            assert cfg["secret_key_ready"] is False
            assert cfg["revision"] == 0
            assert any("主密钥文件已损坏" in w for w in cfg["warnings"])

            # 允许更新非敏感分区（system / default_llm / pricing）
            sys_payload = {**cfg["system"], "expected_revision": 0, "site_notice": "例行公告", "max_pages": 150}
            updated = client.put("/api/admin/config/system", json=sys_payload, headers=headers)
            assert updated.status_code == 200, updated.text
            assert updated.json()["revision"] == 1
            assert client.get("/api/config").json()["max_pages"] == 150
            assert client.get("/api/config").json()["site_notice"] == "例行公告"

            # 禁止保存自定义密钥
            blocked = client.put(
                "/api/admin/config/parser",
                json={
                    "expected_revision": 1,
                    "mineru_base_url": "https://mineru.net",
                    "mineru_token": {"action": "replace", "value": "mineru-secret-123456"},
                },
                headers=headers,
            )
            assert blocked.status_code == 400
            assert "主密钥" in blocked.json()["detail"]
    finally:
        _close_system(client, c)


def test_encrypted_save_hot_reload_url_binding_and_optimistic_lock(authed_system) -> None:
    client, c, headers, _ = authed_system
    raw_free_key = "nvapi-synthetic-free-secret-998877"
    raw_mineru_token = "mineru-synthetic-token-abcdef123456"
    raw_proxy = "socks5h://proxyuser:proxypass123@proxy.example.com:1080"

    # 1. 保存解析服务 Token 与含账密代理
    r_parser = client.put(
        "/api/admin/config/parser",
        json={
            "expected_revision": 0,
            "mineru_base_url": "https://mineru.net",
            "mineru_token": {"action": "replace", "value": raw_mineru_token},
            "mineru_proxy": {"action": "replace", "value": raw_proxy},
            "paddle_public_enabled": True,
        },
        headers=headers,
    )
    assert r_parser.status_code == 200, r_parser.text
    body_parser = r_parser.json()
    assert body_parser["revision"] == 1
    assert body_parser["parser"]["mineru_token"]["configured"] is True
    assert body_parser["parser"]["mineru_proxy"]["mask"] == "socks5h://***@proxy.example.com:1080"
    assert raw_mineru_token not in r_parser.text
    assert "proxypass123" not in r_parser.text

    # 2. 保存免费翻译渠道并开启
    r_free = client.put(
        "/api/admin/config/free_translation",
        json={
            "expected_revision": 1,
            "enabled": True,
            "max_pages": 30,
            "notice": "仅限公开论文体验",
            "providers": [
                {
                    "id": "nim-primary",
                    "name": "NIM 主线",
                    "enabled": True,
                    "protocol": "openai",
                    "base_url": "https://integrate.api.nvidia.com/v1",
                    "model": "nvidia/nemotron-3-super-120b-a12b",
                    "fallback_model": "nvidia/nemotron-3-nano",
                    "timeout_s": 65,
                    "api_key": {"action": "replace", "value": raw_free_key},
                }
            ],
        },
        headers=headers,
    )
    assert r_free.status_code == 200, r_free.text
    assert r_free.json()["revision"] == 2
    assert r_free.json()["free_translation"]["ready"] is True
    assert raw_free_key not in r_free.text

    # 3. /api/config 立刻热生效且不泄露任何密钥或代理账密
    pub = client.get("/api/config")
    assert pub.status_code == 200
    pub_json = pub.json()
    assert pub_json["free_channel"]["enabled"] is True
    assert pub_json["free_channel"]["model_display"] == "NIM 主线"
    assert pub_json["free_channel"]["max_pages"] == 30
    assert pub_json["server_has_parser_token"] is True
    assert pub_json["paddle_public_enabled"] is True
    for secret in (raw_free_key, raw_mineru_token, "proxypass123", "proxyuser"):
        assert secret not in pub.text

    # 4. 改地址时 keep 旧密钥必须被 400 拒绝（防止密钥发往新地址）
    bad_keep = client.put(
        "/api/admin/config/free_translation",
        json={
            "expected_revision": 2,
            "enabled": True,
            "providers": [
                {
                    "id": "nim-primary",
                    "name": "NIM 主线",
                    "enabled": True,
                    "protocol": "openai",
                    "base_url": "https://evil-new-host.example.com/v1",
                    "model": "nvidia/nemotron-3-super-120b-a12b",
                    "api_key": {"action": "keep"},
                }
            ],
        },
        headers=headers,
    )
    assert bad_keep.status_code == 400
    assert "修改服务地址或协议时必须同时更换或禁用原密钥" in bad_keep.json()["detail"]

    # 在线测试接口改地址 keep 原密钥也必须被 400 拒绝
    bad_test = client.post(
        "/api/admin/config/test",
        json={
            "target": "free_provider",
            "provider_id": "nim-primary",
            "protocol": "openai",
            "base_url": "https://evil-new-host.example.com/v1",
            "model": "nvidia/nemotron-3-super-120b-a12b",
            "api_key": {"action": "keep"},
        },
        headers=headers,
    )
    assert bad_test.status_code == 400

    # 5. 乐观锁冲突返回 409
    conflict = client.put(
        "/api/admin/config/system",
        json={"expected_revision": 1, "max_pages": 180},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert "刷新" in conflict.json()["detail"]

    # 6. 数据库落盘审计：runtime_config 存加密串，runtime_config_revisions 零密文、零明文、零账密
    db_path = c.settings.data_dir / "app.db"
    with sqlite3.connect(db_path) as conn:
        config_json, secrets_json = conn.execute(
            "SELECT config_json, secrets_json FROM runtime_config WHERE id = 1"
        ).fetchone()
        revs = conn.execute("SELECT config_json, changed_fields_json, summary FROM runtime_config_revisions").fetchall()
    assert "v1:" in secrets_json
    for secret in (raw_free_key, raw_mineru_token, raw_proxy, "proxypass123", "proxyuser"):
        assert secret not in config_json
        assert secret not in secrets_json
        for row in revs:
            joined = " ".join(str(cell) for cell in row)
            assert secret not in joined
            assert "v1:" not in joined


def test_rollback_safe_and_conflict_blocked(authed_system) -> None:
    client, _, headers, _ = authed_system

    # r1: 配置 MinerU Token + 开启免费渠道（绑定 URL A）
    r1 = client.put(
        "/api/admin/config/parser",
        json={
            "expected_revision": 0,
            "mineru_base_url": "https://mineru.net",
            "mineru_token": {"action": "replace", "value": "mineru-tok-11112222"},
        },
        headers=headers,
    )
    assert r1.status_code == 200
    r2 = client.put(
        "/api/admin/config/free_translation",
        json={
            "expected_revision": 1,
            "enabled": True,
            "providers": [
                {
                    "id": "p1",
                    "name": "线路一",
                    "enabled": True,
                    "protocol": "openai",
                    "base_url": "https://a.example.com/v1",
                    "model": "m1",
                    "api_key": {"action": "replace", "value": "sk-free-aaa-11112222"},
                }
            ],
        },
        headers=headers,
    )
    assert r2.status_code == 200

    # r3: 修改非敏感 system 参数，随后安全回滚到 r2
    r3 = client.put(
        "/api/admin/config/system",
        json={"expected_revision": 2, "site_notice": "临时公告"},
        headers=headers,
    )
    assert r3.status_code == 200 and r3.json()["revision"] == 3
    rolled = client.post(
        "/api/admin/config/rollback",
        json={"target_revision": 2, "expected_revision": 3},
        headers=headers,
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["revision"] == 4
    assert rolled.json()["system"]["site_notice"] == ""

    # r5: 更换 p1 地址为 URL B 并同步更换密钥；此时回滚到 r2（URL A）必须被绑定冲突阻断！
    r5 = client.put(
        "/api/admin/config/free_translation",
        json={
            "expected_revision": 4,
            "enabled": True,
            "providers": [
                {
                    "id": "p1",
                    "name": "线路一",
                    "enabled": True,
                    "protocol": "openai",
                    "base_url": "https://b.example.com/v1",
                    "model": "m1",
                    "api_key": {"action": "replace", "value": "sk-free-bbb-33334444"},
                }
            ],
        },
        headers=headers,
    )
    assert r5.status_code == 200 and r5.json()["revision"] == 5
    blocked = client.post(
        "/api/admin/config/rollback",
        json={"target_revision": 2, "expected_revision": 5},
        headers=headers,
    )
    assert blocked.status_code == 400
    assert "绑定冲突" in blocked.json()["detail"]

    # r6: 关闭免费并禁用密钥；此时回滚到 r5（开启免费）因缺少有效密钥也必须被阻断！
    r6 = client.put(
        "/api/admin/config/free_translation",
        json={
            "expected_revision": 5,
            "enabled": False,
            "providers": [
                {
                    "id": "p1",
                    "name": "线路一",
                    "enabled": False,
                    "protocol": "openai",
                    "base_url": "https://b.example.com/v1",
                    "model": "m1",
                    "api_key": {"action": "disable"},
                }
            ],
        },
        headers=headers,
    )
    assert r6.status_code == 200 and r6.json()["revision"] == 6
    blocked_no_key = client.post(
        "/api/admin/config/rollback",
        json={"target_revision": 5, "expected_revision": 6},
        headers=headers,
    )
    assert blocked_no_key.status_code == 400
    assert "缺少有效渠道密钥" in blocked_no_key.json()["detail"]

    revs = client.get("/api/admin/config/revisions?limit=10&offset=0")
    assert revs.status_code == 200
    assert len(revs.json()["items"]) == 6


def test_corrupted_ciphertext_degrades_gracefully_without_crashing(tmp_path: Path, password_hash: str) -> None:
    key1 = _gen_key()
    key2 = _gen_key()
    client1, c1 = _make_system(tmp_path, password_hash, config_secret_key=key1, mineru_api_token="env-mineru")
    try:
        with client1:
            headers = _login_headers(client1)
            r = client1.put(
                "/api/admin/config/free_translation",
                json={
                    "expected_revision": 0,
                    "enabled": True,
                    "providers": [
                        {
                            "id": "p1",
                            "name": "主渠道",
                            "enabled": True,
                            "protocol": "openai",
                            "base_url": "https://free.example.com/v1",
                            "model": "m1",
                            "api_key": {"action": "replace", "value": "sk-key-one-12345678"},
                        }
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 200
    finally:
        _close_system(client1, c1)

    # 更换主密钥重启：绝不崩溃，免费通道自动降级，后台显示 corrupted 与中文告警
    client2, c2 = _make_system(tmp_path, password_hash, config_secret_key=key2, mineru_api_token="env-mineru")
    try:
        with client2:
            pub = client2.get("/api/config").json()
            assert pub["free_channel"]["enabled"] is False
            _login_headers(client2)
            cfg = client2.get("/api/admin/config").json()
            assert cfg["free_translation"]["ready"] is False
            assert cfg["free_translation"]["providers"][0]["api_key"]["corrupted"] is True
            assert cfg["free_translation"]["providers"][0]["usable"] is False
            assert any("解密失败" in w for w in cfg["warnings"])
    finally:
        _close_system(client2, c2)


def test_overview_ua_stats_connection_test_and_422_redaction(authed_system) -> None:
    client, c, headers, _ = authed_system
    assert c.runtime_config is not None

    # 1. 上报带真实浏览器 UA 的访问记录，验证 /api/admin/overview 含 UA 终端分布
    visit_resp = client.post(
        "/api/stats/visits",
        json={"event_id": str(uuid4()), "visitor_id": str(uuid4())},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
        },
    )
    assert visit_resp.status_code == 204
    overview = client.get("/api/admin/overview")
    assert overview.status_code == 200
    ov = overview.json()
    assert ov["queues"]["parse"]["limit"] >= 1
    assert ov["stats"]["client_breakdown"]["total"] >= 1
    assert any(b["label"] == "desktop" for b in ov["stats"]["client_breakdown"]["devices"])

    # 2. 注入假连接探测器测试 /api/admin/config/test
    async def fake_chat_tester(protocol: str, base_url: str, api_key: str, model: str, timeout_s: int):
        assert api_key == "sk-draft-probe-9999"
        return ConnectionTestOut(ok=True, status_code=200, latency_ms=12, detail="连接正常")

    c.runtime_config._chat_tester = fake_chat_tester
    tested = client.post(
        "/api/admin/config/test",
        json={
            "target": "free_provider",
            "protocol": "openai",
            "base_url": "https://probe.example.com/v1",
            "model": "test-model",
            "api_key": {"action": "replace", "value": "sk-draft-probe-9999"},
        },
        headers=headers,
    )
    assert tested.status_code == 200
    assert tested.json()["ok"] is True

    # 3. 422 脱敏验证：非法字段与敏感密钥绝不回显在 422 响应体中
    secret_marker = "sk-never-echo-secret-422"
    bad_422 = client.put(
        "/api/admin/config/free_translation",
        json={
            "expected_revision": 0,
            "enabled": True,
            "providers": [
                {
                    "id": "INVALID ID!",
                    "name": "bad",
                    "base_url": "https://probe.example.com/v1",
                    "model": "m",
                    "api_key": {"action": "replace", "value": secret_marker},
                }
            ],
        },
        headers=headers,
    )
    assert bad_422.status_code == 422
    assert secret_marker not in bad_422.text
    assert "input" not in bad_422.text

    # 4. 术语库重载接口
    reloaded = client.post("/api/admin/glossary/reload", json={}, headers=headers)
    assert reloaded.status_code == 200
    assert "available" in reloaded.json()


def test_env_mineru_token_bound_to_default_url_on_save_test_and_rollback(tmp_path: Path, password_hash: str) -> None:
    key = _gen_key()
    client, c = _make_system(tmp_path, password_hash, config_secret_key=key, mineru_api_token="env-secret-mineru-tok")
    probe_calls: list[tuple[str, str, str]] = []
    try:
        with client:
            headers = _login_headers(client)
            assert c.runtime_config is not None

            async def fake_parser_tester(target: str, base_url: str, token: str, proxy: str) -> tuple[int, int]:
                probe_calls.append((target, base_url, token))
                return 200, 10

            c.runtime_config._parser_tester = fake_parser_tester

            # 1. 初始状态（数据库从未保存槽位），尝试改 mineru_base_url 且 keep .env Token 必须被拒
            bad_save = client.put(
                "/api/admin/config/parser",
                json={"expected_revision": 0, "mineru_base_url": "https://evil-mineru.example.com"},
                headers=headers,
            )
            assert bad_save.status_code == 400
            assert "环境变量中的默认 Token" in bad_save.json()["detail"]

            # 2. 草稿测试新地址（keep / inherit_env）必须被拒且绝不发出带 .env Token 的请求
            for token_action in ({"action": "keep"}, {"action": "inherit_env"}):
                bad_test = client.post(
                    "/api/admin/config/test",
                    json={
                        "target": "mineru",
                        "base_url": "https://evil-mineru.example.com",
                        "token": token_action,
                    },
                    headers=headers,
                )
                assert bad_test.status_code == 400
                assert "环境变量中的默认 Token" in bad_test.json()["detail"]
            assert probe_calls == []

            # 3. 显式替换为自定义 Token 可保存新地址 (r1)；再改回默认地址并恢复 inherit_env (r2)；
            # 此时回滚到 r1（非默认地址）必须被拒！
            r1 = client.put(
                "/api/admin/config/parser",
                json={
                    "expected_revision": 0,
                    "mineru_base_url": "https://custom-mineru.example.com",
                    "mineru_token": {"action": "replace", "value": "custom-tok-123456"},
                },
                headers=headers,
            )
            assert r1.status_code == 200 and r1.json()["revision"] == 1
            r2 = client.put(
                "/api/admin/config/parser",
                json={
                    "expected_revision": 1,
                    "mineru_base_url": "https://mineru.net",
                    "mineru_token": {"action": "inherit_env"},
                },
                headers=headers,
            )
            assert r2.status_code == 200 and r2.json()["revision"] == 2
            bad_rb = client.post(
                "/api/admin/config/rollback",
                json={"target_revision": 1, "expected_revision": 2},
                headers=headers,
            )
            assert bad_rb.status_code == 400
            assert "环境变量中的默认 Token" in bad_rb.json()["detail"]
            assert probe_calls == []
    finally:
        _close_system(client, c)


def test_default_llm_url_change_updates_server_has_llm_key_and_empty_pricing_and_redaction(
    tmp_path: Path, password_hash: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    from pdftr.parsing import mineru_cloud

    key = _gen_key()
    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            admin_password_hash=password_hash,
            admin_origin=ORIGIN,
            trusted_proxy_cidrs=("127.0.0.1/32",),
            config_secret_key=key,
            llm_api_key="env-shared-llm-key",
            mineru_api_token="env-mineru-token",
        )
    )
    client = TestClient(create_app(c), base_url=ORIGIN)
    try:
        with client:
            headers = _login_headers(client)
            # P1-02: 默认地址与 .env 一致时 server_has_llm_key 为 True，热改地址后立即为 False
            assert client.get("/api/config").json()["server_has_llm_key"] is True
            r_llm = client.put(
                "/api/admin/config/default_llm",
                json={
                    "expected_revision": 0,
                    "base_url": "https://other-llm.example.com/v1",
                    "protocol": "openai",
                    "model": "custom-m",
                    "presets": [],
                },
                headers=headers,
            )
            assert r_llm.status_code == 200
            assert client.get("/api/config").json()["server_has_llm_key"] is False

            # P1-09: 管理员清空计价表保存后，deepseek-flash 不回退内置单价
            est_default = client.get("/api/estimate", params={"pages": 10, "model": "deepseek-flash"}).json()
            assert est_default["cny_low"] is not None
            r_price = client.put(
                "/api/admin/config/pricing",
                json={"expected_revision": 1, "models": []},
                headers=headers,
            )
            assert r_price.status_code == 200
            est_empty = client.get("/api/estimate", params={"pages": 10, "model": "deepseek-flash"}).json()
            assert est_empty["cny_low"] is None and est_empty["cny_high"] is None

            # P0-04: 非有限单价（通过原始 JSON 传 Infinity / NaN 或非法值）必须拒绝且 revision 不变
            bad_inf = client.put(
                "/api/admin/config/pricing",
                content='{"expected_revision":2,"models":[{"model":"m","cache_hit":Infinity,"cache_miss":1,"output":2,"peak_factor":1}]}',
                headers={**headers, "Content-Type": "application/json"},
            )
            assert bad_inf.status_code in (400, 422)
            assert client.get("/api/admin/config").json()["revision"] == 2

            # P1-03: 含凭据标记的畸形代理 URL 绝不回显凭据
            bad_proxy = client.put(
                "/api/admin/config/parser",
                json={
                    "expected_revision": 2,
                    "mineru_base_url": "https://mineru.net",
                    "mineru_proxy": {
                        "action": "replace",
                        "value": "socks5h://u:SECRET_PROXY_PASS_999@[bad-ipv6:1080",
                    },
                },
                headers=headers,
            )
            assert bad_proxy.status_code == 400
            assert "SECRET_PROXY_PASS_999" not in bad_proxy.text

            # P1-07 & P1-03: 解析器测试遇到 503/302 返回 ok=False，鉴权失败不回显上游 msg
            orig_init = mineru_cloud.MinerUCloudParser.__init__
            for status, payload in (
                (503, None),
                (302, None),
                (200, {"code": "A0202", "msg": "UPSTREAM_SECRET_MSG_888"}),
            ):
                transport = httpx2.MockTransport(
                    lambda _r, s=status, p=payload: (
                        httpx2.Response(s, json=p)
                        if p is not None
                        else httpx2.Response(s, headers={"location": "https://redir.example"})
                    )
                )

                def _patched_init(self, *args, _t=transport, **kwargs):
                    kwargs["transport"] = _t
                    orig_init(self, *args, **kwargs)

                monkeypatch.setattr(mineru_cloud.MinerUCloudParser, "__init__", _patched_init)
                res = client.post(
                    "/api/admin/config/test",
                    json={"target": "mineru"},
                    headers=headers,
                )
                assert res.status_code == 200
                body = res.json()
                assert body["ok"] is False
                assert "UPSTREAM_SECRET_MSG_888" not in res.text
    finally:
        _close_system(client, c)


def test_glossary_loader_generation_prevents_stale_overwrite_and_limits_revision_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from pdftr.container import GlossaryLoader
    from pdftr.glossary import sources as glossary_sources
    from pdftr.pipeline.runner import Pipeline

    started = threading.Event()
    release = threading.Event()

    def slow_load_all(targets, vendor_dir):
        del vendor_dir
        started.set()
        release.wait(timeout=3.0)
        return {"gradient": "梯度"}, list(targets)

    monkeypatch.setattr(glossary_sources, "load_all", slow_load_all)
    loader = GlossaryLoader(Settings(_env_file=None, data_dir=tmp_path, glossary_sources=""))  # type: ignore[call-arg]
    loader.reload_sources(["d2l"])
    assert started.wait(timeout=2.0)

    # 在旧线程缓慢加载 d2l 期间，管理员清空来源
    loader.reload_sources([])
    release.set()
    if loader._reload_worker is not None:
        loader._reload_worker.join(timeout=3.0)

    count, reloading, loaded_ids = loader.status()
    assert count == 0
    assert reloading is False
    assert loaded_ids == []
    assert loader() is None

    # 验证 Pipeline.apply_runtime_limits 忽略迟到旧 revision 回调
    c = build_container(Settings(_env_file=None, data_dir=tmp_path / "p", glossary_sources=""))  # type: ignore[call-arg]
    try:
        p = c.pipeline
        assert isinstance(p, Pipeline)
        assert p.apply_runtime_limits(6, 3, 2, revision=5) is True
        assert p.apply_runtime_limits(1, 1, 1, revision=3) is False  # 迟到旧版本被忽略
        q = p.queue_snapshot()
        assert q["parse"][1] == 6 and q["byok"][1] == 3 and q["free"][1] == 2
    finally:
        c.close()


def test_corrupted_single_secret_slot_degrades_without_env_fallback_and_overview_without_audit(
    tmp_path: Path, password_hash: str
) -> None:
    import json

    from pdftr.domain.job import Job, JobStatus

    key = _gen_key()
    client1, c1 = _make_system(
        tmp_path, password_hash, config_secret_key=key, mineru_api_token="env-mineru-should-not-leak"
    )
    try:
        with client1:
            headers = _login_headers(client1)
            r = client1.put(
                "/api/admin/config/free_translation",
                json={
                    "expected_revision": 0,
                    "enabled": True,
                    "providers": [
                        {
                            "id": "good-p",
                            "name": "完好渠道",
                            "enabled": True,
                            "protocol": "openai",
                            "base_url": "https://good.example.com/v1",
                            "model": "m1",
                            "api_key": {"action": "replace", "value": "sk-good-provider-key-1234"},
                        }
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 200
    finally:
        _close_system(client1, c1)

    # 破坏 mineru_token 槽位元数据，保持 free_provider:good-p 完好
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        raw = json.loads(conn.execute("SELECT secrets_json FROM runtime_config WHERE id = 1").fetchone()[0])
        raw["mineru_token"] = {"mode": "invalid_broken_mode"}
        conn.execute("UPDATE runtime_config SET secrets_json = ? WHERE id = 1", (json.dumps(raw),))
        conn.commit()

    # 重启（关闭 submission_audit_enabled）：整站正常启动、坏槽降级且绝不回落 .env、好槽照常可用
    client2, c2 = _make_system(
        tmp_path, password_hash, config_secret_key=key, mineru_api_token="env-mineru-should-not-leak"
    )
    try:
        with client2:
            _login_headers(client2)
            cfg = client2.get("/api/admin/config").json()
            assert cfg["parser"]["mineru_token"]["corrupted"] is True
            assert cfg["parser"]["mineru_token"]["configured"] is False
            assert cfg["free_translation"]["providers"][0]["usable"] is True
            assert c2.runtime_config is not None
            assert c2.runtime_config.snapshot().mineru_token.value == ""

            # 关闭审计时，插入失败任务并上报无 UA 访问，验证概况统计兜底、脱敏与 unknown 桶
            failed_job = Job(
                id="job-failed-no-audit",
                filename="secret_paper.pdf",
                page_count=3,
                status=JobStatus.FAILED,
                stage="translate",
                error="翻译服务请求超时",
                channel_mode="free",
            )
            c2.pipeline.deps.store.create_job(failed_job, None)
            c2.pipeline.deps.store.record_visit(str(uuid4()), str(uuid4()), user_agent=None)

            ov = client2.get("/api/admin/overview").json()
            assert ov["stats"]["failed_24h"] >= 1
            assert ov["stats"]["recent_failures"][0]["job_id"] == "job-failed-no-audit"
            # 精简失败摘要不含 IP 与原始 UA
            assert "submitter_ip" not in ov["stats"]["recent_failures"][0]
            assert "user_agent" not in ov["stats"]["recent_failures"][0]
            assert ov["stats"]["client_breakdown_source"] == "job"
            assert any(b["label"] == "unknown" for b in ov["stats"]["client_breakdown"]["devices"])

            # P2-02: OpenAPI 显式包含全部 6 个分区强类型路由
            openapi = client2.get("/openapi.json").json()
            for sec in ("free_translation", "parser", "default_llm", "pricing", "glossary", "system"):
                assert f"/api/admin/config/{sec}" in openapi["paths"]
    finally:
        _close_system(client2, c2)


def test_create_app_closes_owned_container_on_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed = {"count": 0}
    real_build = build_container
    orig_close = Container.close

    def _wrapped_close(self: Container) -> None:
        closed["count"] += 1
        orig_close(self)

    monkeypatch.setattr(Container, "close", _wrapped_close)
    monkeypatch.setattr(
        "pdftr.api.app.build_container",
        lambda: real_build(Settings(_env_file=None, data_dir=tmp_path, glossary_sources="")),  # type: ignore[call-arg]
    )
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
    assert closed["count"] == 1


def test_paddle_connection_test_rejects_html_404_arrays_empty_and_unknown_codes(
    authed_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    from pdftr.parsing import paddle_vl

    client, _, headers, _ = authed_system
    # 配置一个站点 Paddle Token 以便走真实 PaddleVLParser.check_connection
    r_cfg = client.put(
        "/api/admin/config/parser",
        json={
            "expected_revision": 0,
            "mineru_base_url": "https://mineru.net",
            "paddle_token": {"action": "replace", "value": "paddle-probe-tok-123456"},
        },
        headers=headers,
    )
    assert r_cfg.status_code == 200
    orig_init = paddle_vl.PaddleVLParser.__init__

    def _run_probe(resp: httpx2.Response) -> dict:
        transport = httpx2.MockTransport(lambda _r: resp)

        def _patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            orig_init(self, *args, **kwargs)

        monkeypatch.setattr(paddle_vl.PaddleVLParser, "__init__", _patched)
        res = client.post("/api/admin/config/test", json={"target": "paddle"}, headers=headers)
        assert res.status_code == 200
        return res.json()

    # 必须拒绝：HTML 404、HTTP 200 返回数组 []、空对象 {}、未知业务码 500 / -999、404 带非 404 业务码
    bad_responses = [
        httpx2.Response(404, text="<html>Not Found</html>", headers={"content-type": "text/html"}),
        httpx2.Response(200, json=[]),
        httpx2.Response(200, json={}),
        httpx2.Response(404, json={}),
        httpx2.Response(200, json={"code": 500, "msg": "internal error"}),
        httpx2.Response(200, json={"code": -999, "msg": "unknown"}),
        httpx2.Response(404, json={"code": -999, "msg": "unknown"}),
        httpx2.Response(404, json={"detail": "route missing"}),
    ]
    for resp in bad_responses:
        body = _run_probe(resp)
        assert body["ok"] is False, body

    # 合法业务响应必须通过：HTTP 200 code=0 与 HTTP 404 code=404
    for ok_resp in (
        httpx2.Response(200, json={"code": 0, "data": {}}),
        httpx2.Response(200, json={"errorCode": 0, "result": {}}),
        httpx2.Response(404, json={"code": 404, "msg": "job not found"}),
        httpx2.Response(404, json={"errorCode": 404, "errorMsg": "job not found"}),
    ):
        body = _run_probe(ok_resp)
        assert body["ok"] is True, body


def test_glossary_revision_out_of_order_notification_does_not_overwrite_newer_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pdftr.container import GlossaryLoader
    from pdftr.glossary import sources as glossary_sources

    monkeypatch.setattr(glossary_sources, "load_all", lambda targets, vendor_dir: ({"term": "术语"}, list(targets)))
    loader = GlossaryLoader(Settings(_env_file=None, data_dir=tmp_path, glossary_sources=""))  # type: ignore[call-arg]

    # r2 先到达，迟到的 r1 后到达：r1 必须被直接丢弃，最终生效来源严格保持 r2
    assert loader.reload_sources(["aitd"], revision=2) is True
    if loader._reload_worker is not None:
        loader._reload_worker.join(timeout=3.0)
    assert loader.reload_sources(["d2l"], revision=1) is False
    if loader._reload_worker is not None:
        loader._reload_worker.join(timeout=3.0)
    _, _, loaded_ids = loader.status()
    assert loaded_ids == ["aitd"]

    # 同版本非强制通知忽略；显式 force=True 允许手动重载
    assert loader.reload_sources(["aitd"], revision=2, force=False) is False
    assert loader.reload_sources(["aitd"], revision=2, force=True) is True
    if loader._reload_worker is not None:
        loader._reload_worker.join(timeout=3.0)


def test_legacy_wave3_pricing_without_custom_configured_is_preserved_across_load_estimate_cost_and_rollback(
    tmp_path: Path, password_hash: str
) -> None:
    import json

    from pdftr.domain.llm import ChatUsage
    from pdftr.translation.cost import cost_cny

    key = _gen_key()
    client1, c1 = _make_system(tmp_path, password_hash, config_secret_key=key)
    try:
        with client1:
            headers = _login_headers(client1)
            # 先创建 r1 写入合法表结构
            r1 = client1.put(
                "/api/admin/config/system",
                json={"expected_revision": 0, "site_notice": "v1"},
                headers=headers,
            )
            assert r1.status_code == 200
    finally:
        _close_system(client1, c1)

    # 模拟 Wave 3 旧格式：config_json.pricing 缺少 custom_configured 字段，但含自定义非默认单价
    legacy_pricing = {
        "models": [
            {"model": "deepseek-flash", "cache_hit": 1.0, "cache_miss": 10.0, "output": 40.0, "peak_factor": 1.0}
        ]
    }
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        cfg_raw = json.loads(conn.execute("SELECT config_json FROM runtime_config WHERE id = 1").fetchone()[0])
        cfg_raw["pricing"] = legacy_pricing
        dumped = json.dumps(cfg_raw, ensure_ascii=False)
        conn.execute("UPDATE runtime_config SET config_json = ? WHERE id = 1", (dumped,))
        conn.execute("UPDATE runtime_config_revisions SET config_json = ? WHERE revision = 1", (dumped,))
        conn.commit()

    client2, c2 = _make_system(tmp_path, password_hash, config_secret_key=key)
    try:
        with client2:
            headers2 = _login_headers(client2)
            assert c2.runtime_config is not None
            snap = c2.runtime_config.snapshot()
            assert snap.data.pricing.custom_configured is True
            assert snap.price_overrides is not None
            assert snap.price_overrides["deepseek-flash"].output == 40.0

            # 实际任务计费使用自定义非默认热价表（10 + 40 = 50.0）
            usage = ChatUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
            assert cost_cny("deepseek-flash", usage, peak=False, prices=snap.price_overrides) == pytest.approx(50.0)

            # /api/estimate 估价按自定义单价放大 10 倍（output 40 vs 内置 4）
            est_custom = client2.get("/api/estimate", params={"pages": 10, "model": "deepseek-flash"}).json()
            assert est_custom["cny_low"] is not None and est_custom["cny_low"] > 0.1

            # 显式保存空价表（r2）返回未知价格 None，不回退内置价表
            r2 = client2.put(
                "/api/admin/config/pricing",
                json={"expected_revision": 1, "models": []},
                headers=headers2,
            )
            assert r2.status_code == 200
            est_empty = client2.get("/api/estimate", params={"pages": 10, "model": "deepseek-flash"}).json()
            assert est_empty["cny_low"] is None and est_empty["cny_high"] is None

            # 回滚到 r1（Wave 3 旧格式历史快照）：仍严格恢复自定义热价表！
            rb = client2.post(
                "/api/admin/config/rollback",
                json={"target_revision": 1, "expected_revision": 2},
                headers=headers2,
            )
            assert rb.status_code == 200
            snap_rb = c2.runtime_config.snapshot()
            assert snap_rb.price_overrides is not None
            assert snap_rb.price_overrides["deepseek-flash"].output == 40.0
    finally:
        _close_system(client2, c2)


@pytest.mark.parametrize("corrupt_payload", ["{not-valid-json", "[]", '{"mineru_token": {"enc": "v1:abc"}}'])
def test_corrupted_secrets_table_or_missing_mode_never_revives_env_credentials(
    tmp_path: Path, password_hash: str, corrupt_payload: str, caplog: pytest.LogCaptureFixture
) -> None:
    key = _gen_key()
    env_mineru = "env-mineru-must-not-revive"
    env_proxy = "socks5h://u:SYNTHETIC_PROXY_PASS_991@[bad-ipv6:1080"
    c1 = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            admin_password_hash=password_hash,
            admin_origin=ORIGIN,
            config_secret_key=key,
            mineru_api_token=env_mineru,
            mineru_proxy=env_proxy,
        )
    )
    client1 = TestClient(create_app(c1), base_url=ORIGIN)
    try:
        with client1:
            headers = _login_headers(client1)
            # 启动加载畸形环境代理也不抛异常且绝不泄露账密
            cfg0 = client1.get("/api/admin/config")
            assert cfg0.status_code == 200
            assert "SYNTHETIC_PROXY_PASS_991" not in cfg0.text
            assert cfg0.json()["parser"]["mineru_proxy"]["mask"] == "***（代理格式异常）"
            r = client1.put(
                "/api/admin/config/system",
                json={"expected_revision": 0, "site_notice": "seed"},
                headers=headers,
            )
            assert r.status_code == 200
    finally:
        _close_system(client1, c1)

    with sqlite3.connect(tmp_path / "app.db") as conn:
        conn.execute("UPDATE runtime_config SET secrets_json = ? WHERE id = 1", (corrupt_payload,))
        conn.commit()

    with caplog.at_level("DEBUG"):
        c2 = build_container(
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                data_dir=tmp_path,
                glossary_sources="",
                admin_password_hash=password_hash,
                admin_origin=ORIGIN,
                config_secret_key=key,
                mineru_api_token=env_mineru,
                mineru_proxy="http://u:SYNTHETIC_PROXY_PASS_991@8.8.8.8:8080",
            )
        )
        client2 = TestClient(create_app(c2), base_url=ORIGIN)
        try:
            with client2:
                headers2 = _login_headers(client2)
                assert c2.runtime_config is not None
                snap = c2.runtime_config.snapshot()
                assert snap.mineru_token.value == ""
                assert snap.mineru_token.mode == "disabled"
                assert snap.mineru_token.corrupted is True
                assert snap.warnings
                cfg = client2.get("/api/admin/config").json()
                assert cfg["parser"]["mineru_token"]["mode"] == "disabled"
                assert cfg["parser"]["mineru_token"]["corrupted"] is True
                assert cfg["parser"]["mineru_token"]["configured"] is False
                assert cfg["warnings"]
                if corrupt_payload in ("{not-valid-json", "[]"):
                    assert snap.mineru_proxy.value == ""
                    assert snap.mineru_proxy.corrupted is True
                    assert snap.mineru_proxy.mode == "disabled"
                # 即使随后保存无关的 system 分区，损坏槽仍保持停用，绝不复活 .env 凭据
                r_sys = client2.put(
                    "/api/admin/config/system",
                    json={"expected_revision": 1, "site_notice": "after-corrupt"},
                    headers=headers2,
                )
                assert r_sys.status_code == 200
                assert c2.runtime_config.snapshot().mineru_token.value == ""
        finally:
            _close_system(client2, c2)
    assert "SYNTHETIC_PROXY_PASS_991" not in caplog.text


def test_shutdown_skips_closing_db_when_in_flight_job_exceeds_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from pdftr.domain.job import Job, JobStatus

    closed = {"count": 0}
    write_errors: list[Exception] = []
    started = threading.Event()
    allow_finish = threading.Event()
    real_build = build_container
    orig_close = Container.close
    built: list[Container] = []

    def _wrapped_close(self: Container) -> None:
        closed["count"] += 1
        orig_close(self)

    def _make_owned() -> Container:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            shutdown_grace_s=0.05,
        )
        c = real_build(settings)
        built.append(c)
        return c

    monkeypatch.setattr(Container, "close", _wrapped_close)
    monkeypatch.setattr("pdftr.api.app.build_container", _make_owned)

    async def scenario() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            c = built[0]
            job = Job(id="job-slow-shutdown", filename="slow.pdf", page_count=1)
            c.pipeline.deps.store.create_job(job)

            async def long_worker() -> None:
                started.set()
                await asyncio.to_thread(allow_finish.wait, 2.0)
                try:
                    job.status = JobStatus.DONE
                    c.pipeline.deps.store.save_job(job)
                except Exception as exc:
                    write_errors.append(exc)

            task = asyncio.create_task(long_worker())
            c.pipeline._tasks[job.id] = task
            await asyncio.to_thread(started.wait, 1.0)
        # lifespan 已超时退出但任务仍在运行：绝不能提前关闭 SQLite 连接
        assert closed["count"] == 0
        allow_finish.set()
        await task
        assert write_errors == []
        orig_close(built[0])

    asyncio.run(scenario())
