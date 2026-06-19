"""Backward-compat re-export. Use axiom.shared.keyring instead."""
from axiom.shared.keyring import (  # noqa: F401
    KeyringError,
    encrypt_secret,
    decrypt_secret,
    mask_secret,
)

