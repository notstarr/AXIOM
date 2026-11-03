"""设备侧 token 的读取与生成。

优先级：环境变量 ``AXIOM_MAC_BRIDGE_TOKEN`` > ``<data_dir>/mac_bridge_token``。

为什么放文件而不是数据库：这是"接不接得上"层面的凭据，跟业务数据无关，
数据库里存它只会让备份/导出多带一个敏感值。文件锁死 0600 即可。
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

ENV_VAR = "AXIOM_MAC_BRIDGE_TOKEN"
FILE_NAME = "mac_bridge_token"


def load_or_create(data_dir: Path) -> str:
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return env

    path = data_dir / FILE_NAME
    existing = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if existing:
        return existing

    data_dir.mkdir(parents=True, exist_ok=True)
    token = "mb_" + secrets.token_urlsafe(32)
    # 先写临时文件再 rename，避免写一半留下空 token
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)
    return token


def verify(token: str, expected: str) -> bool:
    """常数时间比较，防时序侧信道。"""
    if not token or not expected:
        return False
    return hmac.compare_digest(token, expected)
