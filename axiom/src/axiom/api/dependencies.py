from __future__ import annotations

from fastapi import Request

from axiom.composition import Container


def get_container(request: Request) -> Container:
    return request.app.state.container
