"""运行时配置与加密密钥的 SQLite 持久化（WAL + 乐观锁 + 不含密钥的变更历史）。"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pdftr.domain.runtime_config import ConfigRevisionSummary, RuntimeConfigData, SecretMode
from pdftr.storage.connection import connect, retry_busy, write_tx

log = logging.getLogger(__name__)

_CIPHER_VERSION_PREFIX = "v1:"
_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16
CONFIG_SECRET_KEY_FILENAME = ".config_secret.key"


def _decode_urlsafe_32b_key(raw: str) -> bytes:
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("密钥为空")
    padded = cleaned + ("=" * ((-len(cleaned)) % 4))
    try:
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("密钥编码无效") from exc
    if len(key) != 32:
        raise ValueError("密钥长度不是 32 字节")
    canonical = base64.urlsafe_b64encode(key).decode("ascii")
    if canonical != padded:
        raise ValueError("密钥编码不规范")
    return key


def load_or_create_local_secret_key(path: Path) -> tuple[bytes | None, str]:
    """未通过环境变量提供主密钥时，在本地数据目录原子生成并持久化 32 字节 AES-256-GCM 主密钥。

    - 文件不存在：以 0600 权限原子创建（避免并发启动竞态与宽松权限窗口）；
    - 文件已存在且合法：复用同一密钥，保证重启后旧密文正常解密；
    - 文件已存在但损坏：绝不覆盖重建，降级为不可用并返回管理告警；
    - 目录不可写/IO 失败：降级为不可用并返回管理告警，不阻塞服务启动。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw_key = os.urandom(32)
        encoded = (base64.urlsafe_b64encode(raw_key).decode("ascii") + "\n").encode("ascii")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        log.info("已在数据目录自动生成本地加密主密钥文件：%s", path.name)
        return raw_key, ""
    except FileExistsError:
        pass
    except OSError:
        log.warning("无法创建本地加密主密钥文件 %s，已降级暂停加密存储", path, exc_info=True)
        return (
            None,
            "无法创建本地加密主密钥文件，已暂停保存自定义密钥或代理，"
            "请检查数据目录写权限或配置 PDFTR_CONFIG_SECRET_KEY",
        )

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        log.warning("无法读取本地加密主密钥文件 %s，已降级暂停加密存储", path, exc_info=True)
        return (
            None,
            "无法读取本地加密主密钥文件，已暂停保存自定义密钥或代理，请检查文件权限或改用 PDFTR_CONFIG_SECRET_KEY",
        )

    try:
        return _decode_urlsafe_32b_key(content), ""
    except ValueError:
        log.warning("本地加密主密钥文件 %s 内容无效或已损坏，已拒绝覆盖并暂停加密存储", path)
        return (
            None,
            "本地加密主密钥文件已损坏（未自动覆盖以保留排障线索），"
            "请修复或移除后重启，或通过 PDFTR_CONFIG_SECRET_KEY 指定主密钥",
        )


class RevisionConflictError(RuntimeError):
    """乐观锁版本号不匹配时抛出。"""


class SecretDecryptError(RuntimeError):
    """密文格式错误、密钥不匹配或 AAD 校验失败时抛出。"""


def _aad_for_slot(slot_name: str) -> bytes:
    if not slot_name:
        raise ValueError("slot_name 不能为空")
    return f"secret:{slot_name}".encode()


def encrypt_secret(key: bytes, slot_name: str, plaintext: str) -> str:
    """使用 AES-256-GCM 加密密钥，绑定槽位名作为 AAD 防止跨槽替换。"""
    if len(key) != 32:
        raise ValueError("AES-256-GCM 密钥必须为 32 字节")
    aad = _aad_for_slot(slot_name)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return _CIPHER_VERSION_PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(key: bytes, slot_name: str, token: str) -> str:
    """解密 AES-256-GCM 密文并校验槽位 AAD；任何异常统一抛出 SecretDecryptError。"""
    if len(key) != 32 or not slot_name:
        raise SecretDecryptError("密钥或槽位名无效")
    if not token.startswith(_CIPHER_VERSION_PREFIX):
        raise SecretDecryptError("密文版本前缀无效")
    encoded = token[len(_CIPHER_VERSION_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretDecryptError("密文 Base64 编码无效") from exc
    if len(raw) < _NONCE_BYTES + _GCM_TAG_BYTES:
        raise SecretDecryptError("密文长度不足")
    nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    try:
        plaintext_bytes = AESGCM(key).decrypt(nonce, ciphertext, _aad_for_slot(slot_name))
        return plaintext_bytes.decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
        raise SecretDecryptError("密文校验或解密失败") from exc


@dataclass
class StoredSecret:
    mode: SecretMode
    enc: str = ""
    mask: str = ""
    bound_url: str = ""
    bound_protocol: str = ""
    corrupted: bool = False


@dataclass
class StoredRuntimeConfig:
    revision: int
    data: RuntimeConfigData
    secrets: dict[str, StoredSecret]
    updated_at: datetime | None
    secrets_corrupted: bool = False


def _decode_secrets(raw_json: str) -> tuple[dict[str, StoredSecret], bool]:
    try:
        parsed: Any = json.loads(raw_json)
    except ValueError:
        log.warning("runtime_config.secrets_json 解析失败，已标记整表损坏并停用全部站点密钥槽")
        return {}, True
    if not isinstance(parsed, dict):
        log.warning("runtime_config.secrets_json 顶层结构无效，已标记整表损坏并停用全部站点密钥槽")
        return {}, True
    result: dict[str, StoredSecret] = {}
    for slot, item in parsed.items():
        if not isinstance(slot, str) or not slot:
            log.warning("runtime_config.secrets_json 存在无效槽位名，已忽略")
            continue
        if not isinstance(item, dict):
            log.warning("密钥槽 %s 的元数据不是对象，已降级停用", slot)
            result[slot] = StoredSecret(mode="disabled", corrupted=True)
            continue
        # 绝不将缺失或非法 mode 默认为 inherit_env，防止损坏槽复活 .env 凭据
        if "mode" not in item:
            log.warning("密钥槽 %s 缺少 mode 字段，已降级停用", slot)
            result[slot] = StoredSecret(mode="disabled", corrupted=True)
            continue
        mode = item.get("mode")
        if mode not in ("custom", "disabled", "inherit_env"):
            log.warning("密钥槽 %s 的模式无效（%r），已降级停用", slot, mode)
            result[slot] = StoredSecret(mode="disabled", corrupted=True)
            continue
        result[slot] = StoredSecret(
            mode=mode,
            enc=str(item.get("enc", "")),
            mask=str(item.get("mask", "")),
            bound_url=str(item.get("bound_url", "")),
            bound_protocol=str(item.get("bound_protocol", "")),
            corrupted=bool(item.get("corrupted", False)),
        )
    return result, False


class SqliteRuntimeConfigStore:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._db = connect(path)
        try:
            self._initialize()
        except BaseException:
            self._db.close()
            raise

    @retry_busy
    def _initialize(self) -> None:
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._lock, write_tx(self._db):
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS runtime_config ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "revision INTEGER NOT NULL, "
                "config_json TEXT NOT NULL, "
                "secrets_json TEXT NOT NULL, "
                "updated_at TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS runtime_config_revisions ("
                "revision INTEGER PRIMARY KEY, "
                "section TEXT NOT NULL, "
                "config_json TEXT NOT NULL, "
                "changed_fields_json TEXT NOT NULL, "
                "summary TEXT NOT NULL, "
                "created_at TEXT NOT NULL)"
            )

    @retry_busy
    def load(self) -> StoredRuntimeConfig | None:
        with self._lock:
            row = self._db.execute(
                "SELECT revision, config_json, secrets_json, updated_at FROM runtime_config WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        secrets, secrets_corrupted = _decode_secrets(row[2])
        return StoredRuntimeConfig(
            revision=int(row[0]),
            data=RuntimeConfigData.model_validate_json(row[1]),
            secrets=secrets,
            updated_at=datetime.fromisoformat(row[3]) if row[3] else None,
            secrets_corrupted=secrets_corrupted,
        )

    @retry_busy
    def save(
        self,
        expected_revision: int,
        data: RuntimeConfigData,
        secrets: dict[str, StoredSecret],
        *,
        section: str,
        changed_fields: list[str],
        summary: str,
    ) -> StoredRuntimeConfig:
        # 仅持久化非敏感配置快照到变更历史表，绝不写入任何密钥明文或密文；
        # 严格禁止 NaN/Infinity 落库，并在写事务前完成无损反序列化自检。
        config_json = json.dumps(data.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)
        RuntimeConfigData.model_validate_json(config_json)
        secrets_json = json.dumps(
            {slot: asdict(secret) for slot, secret in secrets.items()},
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        changed_fields_json = json.dumps(list(changed_fields), ensure_ascii=False, allow_nan=False)
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        with self._lock, write_tx(self._db):
            row = self._db.execute("SELECT revision FROM runtime_config WHERE id = 1").fetchone()
            current_revision = int(row[0]) if row is not None else 0
            if current_revision != expected_revision:
                raise RevisionConflictError(f"配置版本冲突：期望版本 {expected_revision}，当前版本 {current_revision}")
            next_revision = current_revision + 1
            self._db.execute(
                "INSERT INTO runtime_config (id, revision, config_json, secrets_json, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "revision = excluded.revision, "
                "config_json = excluded.config_json, "
                "secrets_json = excluded.secrets_json, "
                "updated_at = excluded.updated_at",
                (next_revision, config_json, secrets_json, now_iso),
            )
            self._db.execute(
                "INSERT INTO runtime_config_revisions "
                "(revision, section, config_json, changed_fields_json, summary, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (next_revision, section, config_json, changed_fields_json, summary, now_iso),
            )

        return StoredRuntimeConfig(
            revision=next_revision,
            data=data.model_copy(deep=True),
            secrets={slot: replace(secret) for slot, secret in secrets.items()},
            updated_at=now,
        )

    @retry_busy
    def list_revisions(self, limit: int = 20, offset: int = 0) -> list[ConfigRevisionSummary]:
        with self._lock:
            rows = self._db.execute(
                "SELECT revision, section, changed_fields_json, summary, created_at "
                "FROM runtime_config_revisions "
                "ORDER BY revision DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [
            ConfigRevisionSummary(
                revision=int(row[0]),
                section=row[1],
                changed_fields=list(json.loads(row[2])),
                summary=row[3],
                created_at=datetime.fromisoformat(row[4]),
            )
            for row in rows
        ]

    @retry_busy
    def get_revision_data(self, revision: int) -> RuntimeConfigData | None:
        with self._lock:
            row = self._db.execute(
                "SELECT config_json FROM runtime_config_revisions WHERE revision = ?",
                (revision,),
            ).fetchone()
        if row is None:
            return None
        return RuntimeConfigData.model_validate_json(row[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()
