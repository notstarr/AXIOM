from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse

from axiom.modules.auth.schemas import (
    AuthStatusResponse,
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
)
from axiom.modules.auth.service import AuthService


router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.container.auth


def extract_token(request: Request, authorization: str | None = None) -> str | None:
    if authorization and authorization.startswith("Bearer "):
        return authorization.split(" ", 1)[1].strip()
    cookie_token = request.cookies.get("axiom_token")
    if cookie_token:
        return cookie_token.strip()
    return None


def client_ip(request: Request) -> str:
    """优先取 X-Forwarded-For 第一个 IP（反代场景），否则直连 IP。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> LoginResponse:
    res = service.login(
        payload.username,
        payload.password,
        remember=payload.remember,
        ip=client_ip(request),
    )
    # 写入 HttpOnly Cookie
    response.set_cookie(
        key="axiom_token",
        value=res.token,
        max_age=res.expires_in_seconds,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return res


@router.get("/status", response_model=AuthStatusResponse)
def auth_status(
    request: Request,
    authorization: str | None = Header(default=None),
    service: AuthService = Depends(get_auth_service),
) -> AuthStatusResponse:
    token = extract_token(request, authorization)
    return service.status(token)


@router.post("/change-password", response_model=LoginResponse)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    service: AuthService = Depends(get_auth_service),
) -> LoginResponse:
    token = extract_token(request, authorization)
    if not token:
        return JSONResponse(status_code=401, content={"error": {"code": "unauthorized", "message": "未提供有效凭据"}})
    
    res = service.change_password(
        token=token,
        old_password=payload.old_password,
        new_password=payload.new_password,
        new_username=payload.new_username,
    )
    response.set_cookie(
        key="axiom_token",
        value=res.token,
        max_age=res.expires_in_seconds,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return res


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(key="axiom_token", path="/")
    return {"ok": True, "message": "已成功退出登录"}
