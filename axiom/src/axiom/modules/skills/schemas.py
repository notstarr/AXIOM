from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator


class Skill(BaseModel):
    name: str
    title: str
    description: str
    path: str
    source: Literal["managed", "bundled"]
    content: str = ""
    enabled: bool = True
    builtin: bool = False


class SkillContent(Skill):
    content: str


class SaveSkill(BaseModel):
    content: str
    description: str | None = None
    enabled: bool | None = None

    @field_validator("content")
    @classmethod
    def require_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Skill content must not be empty")
        return value + "\n"


class SetSkillEnabled(BaseModel):
    enabled: bool


class CreateSkill(BaseModel):
    name: str
    description: str = ""
    content: str = ""
    enabled: bool = True
