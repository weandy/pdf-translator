"""管理员会话、密码格式及端口；不依赖 HTTP 或具体存储。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
SESSION_COOKIE = "__Host-pdftr_admin"
SESSION_SECONDS = 8 * 60 * 60


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str, size: int) -> bytes:
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("管理员密码哈希编码无效") from exc
    if len(result) != size or _encode(result) != value:
        raise ValueError("管理员密码哈希长度或编码无效")
    return result


@dataclass(frozen=True)
class AdminPassword:
    salt: bytes
    digest: bytes

    @classmethod
    def parse(cls, encoded: str) -> AdminPassword:
        parts = encoded.split("$")
        if len(parts) != 7 or parts[:5] != ["scrypt", "v1", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)]:
            raise ValueError("管理员密码哈希必须使用 scrypt v1、N=32768、r=8、p=1")
        return cls(salt=_decode(parts[5], 16), digest=_decode(parts[6], 32))

    def verify(self, password: str) -> bool:
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=self.salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(candidate, self.digest)


def hash_password(password: str) -> str:
    """交互工具和合成测试复用；不接受过短或超出登录上限的密码。"""
    if len(password) < 16 or len(password.encode("utf-8")) > 256:
        raise ValueError("管理密码至少 16 个字符，且不超过 256 个 UTF-8 字节")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt$v1${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_encode(salt)}${_encode(digest)}"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def csrf_for(token: str) -> str:
    return _encode(hmac.digest(token.encode("ascii"), b"pdftr-admin-csrf-v1", "sha256"))


@dataclass(frozen=True)
class AdminSession:
    token_hash: str
    created_at: datetime
    expires_at: datetime
    password_version: str


@dataclass(frozen=True)
class AdminIdentity(AdminSession):
    csrf_token: str


class AdminSessionStore(Protocol):
    def save(self, session: AdminSession) -> None: ...
    def get(self, token_hash: str) -> AdminSession | None: ...
    def delete(self, token_hash: str) -> None: ...
    def purge(self, now: datetime, password_version: str) -> None: ...
    def close(self) -> None: ...
