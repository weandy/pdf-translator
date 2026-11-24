"""运行时配置加密存储、主密钥校验、乐观锁与变更历史防泄露测试。"""

import base64
import os
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdftr.config import Settings, decode_config_secret_key
from pdftr.domain.runtime_config import (
    FreeProviderConfig,
    FreeTranslationConfig,
    RuntimeConfigData,
    mask_proxy_url,
    mask_secret,
)
from pdftr.storage.runtime_config import (
    RevisionConflictError,
    SecretDecryptError,
    SqliteRuntimeConfigStore,
    StoredSecret,
    decrypt_secret,
    encrypt_secret,
)
from pdftr.translation.cost import PRICES


def _make_key(byte_value: int = 0x5A) -> bytes:
    return bytes([byte_value]) * 32


def test_decode_config_secret_key_accepts_padded_and_unpadded_urlsafe_32_bytes() -> None:
    raw_bytes = os.urandom(32)
    padded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
    unpadded = padded.rstrip("=")
    assert padded.endswith("=")
    assert decode_config_secret_key("") is None
    assert decode_config_secret_key(padded) == raw_bytes
    assert decode_config_secret_key(unpadded) == raw_bytes

    settings = Settings(_env_file=None, config_secret_key=padded)  # type: ignore[call-arg]
    assert settings.config_secret_key == padded
    assert padded not in repr(settings)


@pytest.mark.parametrize(
    "bad_key",
    [
        base64.urlsafe_b64encode(b"short-key").decode("ascii"),
        base64.urlsafe_b64encode(b"a" * 31).decode("ascii"),
        base64.urlsafe_b64encode(b"a" * 33).decode("ascii"),
        base64.b64encode(bytes([0xFB]) * 32).decode("ascii"),  # 含标准 Base64 '+' '/'
        "not-valid-base64!@#$",
        " " + base64.urlsafe_b64encode(b"a" * 32).decode("ascii"),
        base64.urlsafe_b64encode(b"a" * 32).decode("ascii") + "=",
    ],
)
def test_decode_config_secret_key_rejects_invalid_values(bad_key: str) -> None:
    with pytest.raises(ValueError, match="32 字节 URL-safe Base64 密钥"):
        decode_config_secret_key(bad_key)
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, config_secret_key=bad_key)  # type: ignore[call-arg]
    assert bad_key not in str(exc_info.value)


def test_domain_defaults_and_masking_rules() -> None:
    data = RuntimeConfigData()
    assert data.free_translation.enabled is False
    assert data.parser.mineru_base_url == "https://mineru.net"
    assert [preset.id for preset in data.default_llm.presets] == ["deepseek", "openai", "anthropic"]
    assert data.glossary.sources == ["aitd", "d2l", "ml-technical-term", "ais-vocab", "computerese"]

    price_map = {entry.model: entry for entry in data.pricing.models}
    for model_name, builtin in PRICES.items():
        assert model_name in price_map
        entry = price_map[model_name]
        assert entry.cache_hit == builtin.cache_hit
        assert entry.cache_miss == builtin.cache_miss
        assert entry.output == builtin.output
        assert entry.peak_factor == builtin.peak_factor

    assert mask_secret("") == ""
    assert mask_secret("short") == "****"
    assert mask_secret("abcdefgh") == "ab...gh"
    masked_nvapi = mask_secret("nvapi-1234567890abcdef")
    assert masked_nvapi == "nvap...cdef"
    assert "nvapi-" not in masked_nvapi

    assert mask_proxy_url("") == ""
    assert mask_proxy_url("http://proxy.example.com:8080") == "http://proxy.example.com:8080"
    assert mask_proxy_url("socks5h://alice:s3cr3t@proxy.example.com:1080") == "socks5h://***@proxy.example.com:1080"
    assert mask_proxy_url("socks5://user:pass@[2001:db8::1]:1080") == "socks5://***@[2001:db8::1]:1080"
    # 格式畸形的含账密代理绝不抛异常也不回显任何账密字符
    for malformed in (
        "socks5h://user:SYNTHETIC_PROXY_PASS_991@[bad-ipv6:1080",
        "http://user:SYNTHETIC_PROXY_PASS_991@proxy.example.com:badport",
        "not-a-valid-proxy-SYNTHETIC_PROXY_PASS_991",
    ):
        masked = mask_proxy_url(malformed)
        assert masked == "***（代理格式异常）"
        assert "SYNTHETIC_PROXY_PASS_991" not in masked


def test_free_provider_id_validation_and_uniqueness() -> None:
    p1 = FreeProviderConfig(
        id="nim-primary",
        name="NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        model="deepseek-ai/deepseek-v3",
    )
    cfg = FreeTranslationConfig(enabled=True, providers=[p1])
    assert cfg.providers[0].timeout_s == 65

    with pytest.raises(ValidationError):
        FreeProviderConfig(
            id="Invalid_Upper",
            name="Bad",
            base_url="https://example.com/v1",
            model="m1",
        )
    with pytest.raises(ValidationError):
        FreeTranslationConfig(providers=[p1, p1.model_copy()])


def test_aes_gcm_roundtrip_aad_binding_and_tamper_rejection() -> None:
    key = _make_key(0x11)
    wrong_key = _make_key(0x22)
    plaintext = "nvapi-synthetic-secret-token-中文-123456"

    token1 = encrypt_secret(key, "free:nim-primary", plaintext)
    token2 = encrypt_secret(key, "free:nim-primary", plaintext)
    assert token1.startswith("v1:")
    assert token2.startswith("v1:")
    assert token1 != token2
    assert plaintext not in token1
    assert decrypt_secret(key, "free:nim-primary", token1) == plaintext
    assert decrypt_secret(key, "free:nim-primary", token2) == plaintext

    # 空明文也支持合法加解密
    empty_token = encrypt_secret(key, "slot_empty", "")
    assert decrypt_secret(key, "slot_empty", empty_token) == ""

    # AAD 串槽拒绝
    with pytest.raises(SecretDecryptError):
        decrypt_secret(key, "free:other-provider", token1)

    # 错密钥拒绝
    with pytest.raises(SecretDecryptError):
        decrypt_secret(wrong_key, "free:nim-primary", token1)

    # 篡改密文拒绝
    raw = bytearray(base64.urlsafe_b64decode(token1[3:].encode("ascii")))
    raw[-1] ^= 0x01
    tampered = "v1:" + base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(SecretDecryptError):
        decrypt_secret(key, "free:nim-primary", tampered)

    # 非法前缀、非法编码、截断密文、非法密钥长度均拒绝
    for invalid_token in (
        token1.replace("v1:", "v2:", 1),
        "v1:!!!not-base64!!!",
        "v1:" + base64.urlsafe_b64encode(b"too-short").decode("ascii"),
    ):
        with pytest.raises(SecretDecryptError):
            decrypt_secret(key, "free:nim-primary", invalid_token)
    with pytest.raises(SecretDecryptError):
        decrypt_secret(b"short", "free:nim-primary", token1)


def test_store_persistence_optimistic_locking_and_secret_free_history(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    key = _make_key(0x33)
    nvapi_plain = "nvapi-live-synthetic-secret-9988776655"
    proxy_plain = "socks5h://proxyuser:proxypass@proxy.example.com:1080"

    nvapi_enc = encrypt_secret(key, "free:nim-primary", nvapi_plain)
    proxy_enc = encrypt_secret(key, "mineru_proxy", proxy_plain)

    store = SqliteRuntimeConfigStore(db_path)
    try:
        assert store.load() is None
        assert store.list_revisions() == []
        assert store.get_revision_data(1) is None

        # 空表只接受 expected_revision == 0
        with pytest.raises(RevisionConflictError):
            store.save(
                1,
                RuntimeConfigData(),
                {},
                section="system",
                changed_fields=["system.site_notice"],
                summary="错误版本写入",
            )

        data_v1 = RuntimeConfigData(
            free_translation=FreeTranslationConfig(
                enabled=True,
                max_pages=30,
                providers=[
                    FreeProviderConfig(
                        id="nim-primary",
                        name="NVIDIA NIM",
                        base_url="https://integrate.api.nvidia.com/v1",
                        model="deepseek-ai/deepseek-v3",
                    )
                ],
            )
        )
        secrets_v1 = {
            "free:nim-primary": StoredSecret(
                mode="custom",
                enc=nvapi_enc,
                mask=mask_secret(nvapi_plain),
                bound_url="https://integrate.api.nvidia.com/v1",
                bound_protocol="openai",
            ),
            "mineru_proxy": StoredSecret(
                mode="custom",
                enc=proxy_enc,
                mask=mask_proxy_url(proxy_plain),
            ),
        }

        saved_v1 = store.save(
            0,
            data_v1,
            secrets_v1,
            section="free_translation",
            changed_fields=["free_translation.enabled", "free_translation.providers"],
            summary="启用免费翻译渠道",
        )
        assert saved_v1.revision == 1
        assert saved_v1.updated_at is not None

        # 旧版本写入触发乐观锁冲突，不破坏已存数据
        with pytest.raises(RevisionConflictError):
            store.save(
                0,
                RuntimeConfigData(),
                {},
                section="system",
                changed_fields=["system.maintenance_mode"],
                summary="并发冲突写入",
            )

        data_v2 = data_v1.model_copy(deep=True)
        data_v2.system.maintenance_mode = True
        data_v2.system.site_notice = "系统维护中"
        saved_v2 = store.save(
            1,
            data_v2,
            secrets_v1,
            section="system",
            changed_fields=["system.maintenance_mode", "system.site_notice"],
            summary="开启维护模式",
        )
        assert saved_v2.revision == 2

        revisions = store.list_revisions(limit=10, offset=0)
        assert [rev.revision for rev in revisions] == [2, 1]
        assert revisions[0].section == "system"
        assert revisions[0].changed_fields == ["system.maintenance_mode", "system.site_notice"]
        assert revisions[1].summary == "启用免费翻译渠道"

        paged = store.list_revisions(limit=1, offset=1)
        assert [rev.revision for rev in paged] == [1]

        snap_v1 = store.get_revision_data(1)
        snap_v2 = store.get_revision_data(2)
        assert snap_v1 is not None and snap_v1.system.maintenance_mode is False
        assert snap_v2 is not None and snap_v2.system.maintenance_mode is True
    finally:
        store.close()

    # 重新打开数据库校验持久化完整性
    reopened = SqliteRuntimeConfigStore(db_path)
    try:
        loaded = reopened.load()
        assert loaded is not None
        assert loaded.revision == 2
        assert loaded.data.system.maintenance_mode is True
        assert decrypt_secret(key, "free:nim-primary", loaded.secrets["free:nim-primary"].enc) == nvapi_plain
        assert decrypt_secret(key, "mineru_proxy", loaded.secrets["mineru_proxy"].enc) == proxy_plain
    finally:
        reopened.close()

    # 严格审计历史表：绝不包含任何明文密钥、密文、v1: 前缀、nvapi- 或代理账号密码
    with sqlite3.connect(db_path) as db:
        history_rows = db.execute("SELECT * FROM runtime_config_revisions").fetchall()
        history_blob = "\n".join(str(row) for row in history_rows)
        for forbidden in (nvapi_plain, nvapi_enc, proxy_plain, proxy_enc, "v1:", "nvapi-", "proxyuser", "proxypass"):
            assert forbidden not in history_blob

        config_row = db.execute("SELECT config_json, secrets_json FROM runtime_config WHERE id = 1").fetchone()
        assert config_row is not None
        assert nvapi_plain not in config_row[0]
        assert nvapi_plain not in config_row[1]
        assert proxy_plain not in config_row[1]
        assert "proxypass" not in config_row[1]


@pytest.mark.parametrize("bad_num", [float("inf"), float("-inf"), float("nan")])
def test_pricing_rejects_non_finite_numbers(bad_num: float) -> None:
    from pdftr.domain.runtime_config import ModelPriceEntry

    for field in ("cache_hit", "cache_miss", "output", "peak_factor"):
        payload = {"model": "m1", "cache_hit": 0.1, "cache_miss": 1.0, "output": 2.0, "peak_factor": 1.0}
        payload[field] = bad_num
        with pytest.raises(ValidationError):
            ModelPriceEntry.model_validate(payload)


def test_decode_secrets_degrades_single_corrupted_slot_without_blocking_other_slots(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    key = _make_key(0x77)
    good_enc = encrypt_secret(key, "mineru_token", "good-mineru-token-123456")
    store = SqliteRuntimeConfigStore(db_path)
    try:
        store.save(
            0,
            RuntimeConfigData(),
            {
                "mineru_token": StoredSecret(
                    mode="custom",
                    enc=good_enc,
                    mask=mask_secret("good-mineru-token-123456"),
                    bound_url="https://mineru.net",
                    bound_protocol="mineru",
                ),
                "paddle_token": StoredSecret(mode="disabled"),
            },
            section="parser",
            changed_fields=["parser"],
            summary="初始保存",
        )
    finally:
        store.close()

    # 手工破坏 paddle_token 槽位（非法 mode）与额外坏槽（非 dict），保留 mineru_token 完好
    with sqlite3.connect(db_path) as conn:
        import json

        raw = json.loads(conn.execute("SELECT secrets_json FROM runtime_config WHERE id = 1").fetchone()[0])
        raw["paddle_token"] = {"mode": "unknown_bad_mode"}
        raw["free_provider:bad"] = "not-a-dict"
        conn.execute(
            "UPDATE runtime_config SET secrets_json = ? WHERE id = 1",
            (json.dumps(raw),),
        )
        conn.commit()

    reopened = SqliteRuntimeConfigStore(db_path)
    try:
        loaded = reopened.load()
        assert loaded is not None
        assert loaded.secrets["mineru_token"].corrupted is False
        assert decrypt_secret(key, "mineru_token", loaded.secrets["mineru_token"].enc) == "good-mineru-token-123456"
        assert loaded.secrets["paddle_token"].corrupted is True
        assert loaded.secrets["paddle_token"].mode == "disabled"
        assert loaded.secrets["free_provider:bad"].corrupted is True
    finally:
        reopened.close()


def test_load_or_create_local_secret_key_lifecycle(tmp_path: Path) -> None:
    from pdftr.storage.runtime_config import CONFIG_SECRET_KEY_FILENAME, load_or_create_local_secret_key

    key_file = tmp_path / CONFIG_SECRET_KEY_FILENAME
    assert not key_file.exists()

    # 1. 首次调用自动生成合法 32 字节密钥并持久化
    key1, warn1 = load_or_create_local_secret_key(key_file)
    assert key1 is not None and len(key1) == 32
    assert warn1 == ""
    assert key_file.exists()

    # 2. 再次调用复用同一密钥
    key2, warn2 = load_or_create_local_secret_key(key_file)
    assert key2 == key1
    assert warn2 == ""

    # 3. 文件损坏时绝不静默覆盖，降级返回 None + 告警
    corrupted = tmp_path / "broken.key"
    corrupted.write_text("bad-key-content\n", encoding="utf-8")
    key3, warn3 = load_or_create_local_secret_key(corrupted)
    assert key3 is None
    assert "损坏" in warn3
    assert corrupted.read_text(encoding="utf-8") == "bad-key-content\n"

    # 4. 目标路径为目录（IO 失败）时优雅降级不抛异常
    dir_target = tmp_path / "as_dir.key"
    dir_target.mkdir()
    key4, warn4 = load_or_create_local_secret_key(dir_target)
    assert key4 is None
    assert warn4 != ""
