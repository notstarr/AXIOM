from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    remember: bool = True


class LoginResponse(BaseModel):
    ok: bool = True
    token: str
    username: str
    role: Literal["admin"] = "admin"
    expires_in_seconds: int


class AuthStatusResponse(BaseModel):
    authenticated: bool
    username: str | None = None
    role: str | None = None
    created_at: str | None = None
    has_custom_password: bool = False


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)
    new_username: str | None = Field(default=None, max_length=64)


class AdminUserRecord(BaseModel):
    username: str = "axiom"
    password_hash: str
    salt: str
    created_at: str
    updated_at: str
