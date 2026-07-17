from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class KeyringError(RuntimeError):
    pass


def _fernet() -> Fernet:
    master_key = _load_master_key()
    if not master_key:
        raise KeyringError("MASTER_KEY is not configured")
    digest = hashlib.sha256(master_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _load_master_key() -> str:
    master_key = os.getenv("MASTER_KEY", "").strip()
    if master_key:
        return master_key
    key_file = os.getenv("CAIRN_MASTER_KEY_FILE") or os.getenv("MASTER_KEY_FILE")
    path = Path(key_file).expanduser() if key_file else Path.home() / ".config" / "cairn" / "master.key"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def encrypt_secret(raw_secret: str) -> str:
    secret = raw_secret.strip()
    if not secret:
        raise KeyringError("api key must not be empty")
    token = _fernet().encrypt(secret.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    try:
        plaintext = _fernet().decrypt(ciphertext.encode("utf-8"))
    except InvalidToken as exc:
        raise KeyringError("invalid encrypted api key") from exc
    return plaintext.decode("utf-8")


def mask_secret(secret: str) -> str:
    secret = secret.strip()
    if not secret:
        return ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return f"{'*' * (len(secret) - 4)}{secret[-4:]}"
