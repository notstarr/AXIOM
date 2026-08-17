from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from axiom.modules.auth.schemas import (
    AdminUserRecord,
    AuthStatusResponse,
    LoginResponse,
)
from axiom.shared.clock import utcnow
from axiom.shared.errors import DomainError


class AuthError(DomainError):
    code = "auth_error"
    status_code = 401

    def __init__(self, code: str, message: str, status_code: int = 401):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LoginThrottle:
    """登录爆破防护（进程内）：连续失败锁定 + 单 IP 限速。

    适用于单进程 uvicorn 部署（launchd 场景）。多 worker/多副本部署时
    应把计数迁移到 Redis 等共享存储。
    """

    MAX_CONSECUTIVE_FAILURES = 5
    LOCKOUT_SECONDS = 15 * 60
    PER_MINUTE_LIMIT = 10

    def __init__(self) -> None:
        # (ip, username) -> (fail_count, locked_until_epoch)
        self._failures: dict[tuple[str, str], tuple[int, float]] = {}
        # (ip, minute_window) -> attempt_count
        self._attempts: dict[tuple[str, int], int] = {}

    def check(self, ip: str, username: str) -> None:
        key = (ip, username.strip().lower())
        now = time.time()
        _, locked_until = self._failures.get(key, (0, 0.0))
        if locked_until > now:
            raise AuthError(
                "login_locked",
                "登录失败次数过多，账号已临时锁定，请 15 分钟后再试",
                status_code=429,
            )
        minute_key = (ip, int(now // 60))
        attempts = self._attempts.get(minute_key, 0)
        if attempts >= self.PER_MINUTE_LIMIT:
            raise AuthError(
                "login_rate_limited",
                "登录尝试过于频繁，请稍后再试",
                status_code=429,
            )
        self._attempts[minute_key] = attempts + 1

    def record_failure(self, ip: str, username: str) -> None:
        key = (ip, username.strip().lower())
        now = time.time()
        count, _ = self._failures.get(key, (0, 0.0))
        count += 1
        locked_until = now + self.LOCKOUT_SECONDS if count >= self.MAX_CONSECUTIVE_FAILURES else 0.0
        self._failures[key] = (count, locked_until)

    def record_success(self, ip: str, username: str) -> None:
        key = (ip, username.strip().lower())
        self._failures.pop(key, None)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    )
    return dk.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    )
    return hmac.compare_digest(dk.hex(), password_hash)


class AuthService:
    TOKEN_VALIDITY_SECONDS = 7 * 24 * 3600  # 7 天免登

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.expanduser().resolve()
        self.auth_file = self.data_dir / "auth_admin.json"
        self.secret_file = self.data_dir / "auth_secret.key"
        self._jwt_secret = self._get_or_create_secret()
        self._ensure_admin_user()
        self.login_throttle = LoginThrottle()

    def _get_or_create_secret(self) -> bytes:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.secret_file.exists():
            try:
                secret = self.secret_file.read_bytes().strip()
                if len(secret) >= 32:
                    return secret
            except Exception:
                pass
        secret = secrets.token_bytes(32)
        self.secret_file.write_bytes(secret)
        self.secret_file.chmod(0o600)
        return secret

    def _ensure_admin_user(self) -> None:
        if self.auth_file.exists():
            return
        
        # 优先读取环境变量，其次随机生成或使用默认初始密码
        initial_username = os.environ.get("AXIOM_ADMIN_USER", "axiom").strip() or "axiom"
        initial_password = os.environ.get("AXIOM_ADMIN_PASSWORD", "").strip()
        
        is_random = False
        if not initial_password:
            # 生成 16 位高强度随机密码
            alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*"
            initial_password = "".join(secrets.choice(alphabet) for _ in range(16))
            is_random = True

        pwd_hash, salt = hash_password(initial_password)
        record = AdminUserRecord(
            username=initial_username,
            password_hash=pwd_hash,
            salt=salt,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self._save_record(record)

        # 写入 credentials.txt 方便用户随时查阅
        cred_file = self.data_dir / "credentials.txt"
        try:
            cred_file.write_text(
                f"AXIOM Admin Credentials\n"
                f"Generated At: {utcnow()}\n"
                f"Username: {initial_username}\n"
                f"Password: {initial_password}\n"
            )
            cred_file.chmod(0o600)
        except Exception:
            pass

    def _get_record(self) -> AdminUserRecord:
        if not self.auth_file.exists():
            self._ensure_admin_user()
        try:
            data = json.loads(self.auth_file.read_text("utf-8"))
            return AdminUserRecord.model_validate(data)
        except Exception:
            self._ensure_admin_user()
            data = json.loads(self.auth_file.read_text("utf-8"))
            return AdminUserRecord.model_validate(data)

    def _save_record(self, record: AdminUserRecord) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.auth_file.write_text(record.model_dump_json(indent=2), "utf-8")
        self.auth_file.chmod(0o600)

    def login(
        self,
        username: str,
        password: str,
        remember: bool = True,
        ip: str = "",
    ) -> LoginResponse:
        self.login_throttle.check(ip, username)
        record = self._get_record()
        if username.strip() != record.username or not verify_password(
            password, record.password_hash, record.salt
        ):
            self.login_throttle.record_failure(ip, username)
            raise AuthError("invalid_credentials", "用户名或密码错误", status_code=401)
        self.login_throttle.record_success(ip, username)

        expires_in = self.TOKEN_VALIDITY_SECONDS if remember else 24 * 3600
        token = self._create_token(record.username, expires_in)
        return LoginResponse(
            token=token,
            username=record.username,
            role="admin",
            expires_in_seconds=expires_in,
        )

    def status(self, token: str | None) -> AuthStatusResponse:
        record = self._get_record()
        if not token:
            return AuthStatusResponse(authenticated=False)
        payload = self.verify_token(token)
        if not payload:
            return AuthStatusResponse(authenticated=False)
        return AuthStatusResponse(
            authenticated=True,
            username=record.username,
            role="admin",
            created_at=record.created_at,
            has_custom_password=True,
        )

    def change_password(
        self,
        token: str,
        old_password: str,
        new_password: str,
        new_username: str | None = None,
    ) -> LoginResponse:
        payload = self.verify_token(token)
        if not payload:
            raise AuthError("unauthorized", "身份认证已过期，请重新登录", status_code=401)

        record = self._get_record()
        if not verify_password(old_password, record.password_hash, record.salt):
            raise AuthError("invalid_old_password", "原密码不正确", status_code=400)

        new_hash, new_salt = hash_password(new_password)
        target_username = new_username.strip() if new_username and new_username.strip() else record.username
        updated = AdminUserRecord(
            username=target_username,
            password_hash=new_hash,
            salt=new_salt,
            created_at=record.created_at,
            updated_at=utcnow(),
        )
        self._save_record(updated)

        # 更新 credentials.txt
        cred_file = self.data_dir / "credentials.txt"
        try:
            cred_file.write_text(
                f"AXIOM Admin Credentials (Updated)\n"
                f"Updated At: {utcnow()}\n"
                f"Username: {target_username}\n"
                f"Password: {new_password}\n"
            )
            cred_file.chmod(0o600)
        except Exception:
            pass

        # 颁发新 Token
        new_token = self._create_token(target_username, self.TOKEN_VALIDITY_SECONDS)
        return LoginResponse(
            token=new_token,
            username=target_username,
            role="admin",
            expires_in_seconds=self.TOKEN_VALIDITY_SECONDS,
        )

    def reset_password(self, password: str, username: str = "axiom") -> None:
        new_hash, new_salt = hash_password(password)
        updated = AdminUserRecord(
            username=username,
            password_hash=new_hash,
            salt=new_salt,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self._save_record(updated)
        cred_file = self.data_dir / "credentials.txt"
        try:
            cred_file.write_text(
                f"AXIOM Admin Credentials (Reset)\n"
                f"Updated At: {utcnow()}\n"
                f"Username: {username}\n"
                f"Password: {password}\n"
            )
            cred_file.chmod(0o600)
        except Exception:
            pass

    def _create_token(self, username: str, expires_in: int) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "sub": username,
            "role": "admin",
            "iat": int(time.time()),
            "exp": int(time.time()) + expires_in,
        }
        encoded_header = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        encoded_payload = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        signature = hmac.new(
            self._jwt_secret,
            f"{encoded_header}.{encoded_payload}".encode(),
            hashlib.sha256,
        ).digest()
        encoded_sig = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        return f"{encoded_header}.{encoded_payload}.{encoded_sig}"

    def verify_token(self, token: str) -> dict[str, Any] | None:
        try:
            parts = token.strip().split(".")
            if len(parts) != 3:
                return None
            header_b64, payload_b64, sig_b64 = parts
            expected_sig = hmac.new(
                self._jwt_secret,
                f"{header_b64}.{payload_b64}".encode(),
                hashlib.sha256,
            ).digest()
            actual_sig = base64.urlsafe_b64decode(sig_b64 + "==")
            if not hmac.compare_digest(expected_sig, actual_sig):
                return None
            payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "==").decode())
            if payload.get("exp", 0) < time.time():
                return None
            return payload
        except Exception:
            return None
