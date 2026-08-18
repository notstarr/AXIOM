from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, text

from axiom import __version__
from axiom.api.router import router
from axiom.composition import Container
from axiom.shared.database import AXIOM_SCHEMA
from axiom.shared.errors import DomainError
from axiom.shared.maintenance import read_maintenance

logger = logging.getLogger(__name__)


def _read_service_token(data_dir: Path) -> str:
    """机器控制面（runner）专用凭据：优先 env，其次 service_token 文件。"""
    env_token = os.environ.get("AXIOM_SERVICE_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        token_file = data_dir / "service_token"
        if token_file.exists():
            return token_file.read_text("utf-8").strip()
    except Exception:
        pass
    return ""


# 未登录可公开访问的静态资源白名单（登录卡渲染必需，不含任何业务逻辑/API 路径）
_PUBLIC_STATIC_FILES = {
    "/static/js/login.js",          # 登录网关（仅登录/登出/改密最小逻辑）
    "/static/vendor/alpine.min.js", # Alpine 运行时（x-data 渲染依赖）
}


def _is_public_static(path: str) -> bool:
    """登录卡阶段公开的静态资源：白名单文件 + 样式/图片等不含业务信息的类型。"""
    if path in _PUBLIC_STATIC_FILES:
        return True
    return path.endswith(
        (".css", ".jpg", ".jpeg", ".png", ".svg", ".ico", ".woff", ".woff2", ".ttf")
    )


# 前端脚本 sentinel：app.py 根据登录态替换为完整脚本列表（已登录）或单条 login.js（未登录）
_AUTH_SCRIPTS_SENTINEL = (
    "<!--AXIOM_AUTH_SCRIPTS_START-->\n"
    "{scripts}\n"
    "<!--AXIOM_AUTH_SCRIPTS_END-->"
)
_AUTH_SCRIPTS_PATTERN = (
    r"<!--AXIOM_AUTH_SCRIPTS_START-->.*?<!--AXIOM_AUTH_SCRIPTS_END-->"
)
_UNAUTH_SCRIPT = (
    '<script src="/static/js/login.js?v=20260817-auth-gate-v1"></script>'
)


def _build_index_variants(index_file: Path) -> tuple[bytes, bytes]:
    """启动时一次性生成未登录/已登录两份 HTML 缓存，避免每次请求读盘。

    - 已登录版：保留 index.html 原文（内联完整业务脚本）
    - 未登录版：把 sentinel 段替换为单条 login.js
    """
    import re
    src = index_file.read_text(encoding="utf-8")
    authed_bytes = src.encode("utf-8")
    if "<!--AXIOM_AUTH_SCRIPTS_START-->" in src and "<!--AXIOM_AUTH_SCRIPTS_END-->" in src:
        # 未登录：把 sentinel 段（含换行）替换为单条 login.js
        unauth = re.sub(
            r"<!--AXIOM_AUTH_SCRIPTS_START-->[\s\S]*?<!--AXIOM_AUTH_SCRIPTS_END-->",
            _AUTH_SCRIPTS_SENTINEL.format(scripts=_UNAUTH_SCRIPT),
            src,
            count=1,
        )
        unauth_bytes = unauth.encode("utf-8")
    else:
        # 缺少 sentinel：降级为两份完全相同（兼容回退）
        unauth_bytes = authed_bytes
    return authed_bytes, unauth_bytes


def _extract_user_token(request: Request) -> str:
    """从请求中提取用户/服务 token，与 enforce_auth 中间件规则一致。"""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1].strip()
    cookie_token = request.cookies.get("axiom_token", "").strip()
    return cookie_token or ""


def create_app(*, engine: Engine, data_dir: Path) -> FastAPI:
    data_dir = data_dir.expanduser().resolve()
    app = FastAPI(
        title="AXIOM API",
        version=__version__,
        description="Modular Task/Fact/Intent orchestration backend",
        # 公网安全：关闭自动 API 文档，避免暴露全部接口结构（/docs /redoc /openapi.json）
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    initial_maintenance = read_maintenance(data_dir)
    app.state.container = Container.build(
        engine,
        data_dir=data_dir,
        allow_startup_writes=initial_maintenance.state != "frozen",
    )
    app.state.service_token = _read_service_token(data_dir)
    app.include_router(router)

    # ── 钉钉 Stream 模式机器人：接收群消息并响应 /help 等指令 ──
    # 仅在配置了 AppKey/AppSecret 且启用钉钉时启动（守护线程，断线自动重连）。
    app.state.dingtalk_bot = None

    def _start_dingtalk_stream() -> None:
        try:
            from axiom.modules.settings.service import load_runtime_config
            from axiom.shared.dingtalk import DingTalkStreamBot

            config = load_runtime_config(engine)
            if not (config.dingtalk_enabled and config.dingtalk_app_key.strip() and config.dingtalk_app_secret.strip()):
                return
            bot = DingTalkStreamBot(
                app_key=config.dingtalk_app_key.strip(),
                app_secret=config.dingtalk_app_secret.strip(),
                container=app.state.container,
            )
            bot.start()
            app.state.dingtalk_bot = bot
        except Exception:
            logging.getLogger(__name__).exception("Failed to start DingTalk Stream bot")

    _start_dingtalk_stream()

    @app.middleware("http")
    async def enforce_auth(request: Request, call_next):
        # 测试/开发模式开关：AXIOM_AUTH_DISABLED=1 时跳过鉴权拦截
        if os.environ.get("AXIOM_AUTH_DISABLED") == "1":
            return await call_next(request)
        path = request.url.path
        # ── 1. 无条件放行：健康检查 / 登录接口 / 页面入口 / 登录卡公开资源 / 钉钉Webhook ──
        if (
            path == "/health"
            or path.startswith("/api/v1/auth/login")
            or path.startswith("/api/v1/auth/status")
            or path.startswith("/api/v1/integrations/dingtalk/webhook")
            or path in {"/", "/favicon.svg", "/docs", "/redoc", "/openapi.json"}
        ):
            return await call_next(request)

        is_api = path.startswith("/api/")
        is_static = path.startswith("/static/")

        # ── 2. 非 API 非受控静态（SPA 前端路由）直接放行，由前端 401 兜底 ──
        if not is_api and not is_static:
            return await call_next(request)

        # ── 3. 静态资源精细控制：公开白名单放行，其余（业务 JS/模板/vendor）走鉴权 ──
        if is_static and _is_public_static(path):
            return await call_next(request)

        # 检查 Authorization header 或 Cookie
        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
        elif "axiom_token" in request.cookies:
            token = request.cookies.get("axiom_token", "").strip()

        container: Container = request.app.state.container
        # 用户登录 token
        if token and container.auth.verify_token(token):
            return await call_next(request)
        # 机器控制面 service token（runner 专用，与用户登录态解耦）
        service_token = request.app.state.service_token
        if service_token and token and hmac.compare_digest(token, service_token):
            return await call_next(request)

        if is_static:
            # 受保护静态资源（业务 JS/模板/vendor）伪装 404：不暴露文件存在性与接口清单
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": "Not Found"}},
            )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "unauthorized",
                    "message": "未登录或登录凭据已过期，请重新登录",
                }
            },
        )

    @app.middleware("http")
    async def enforce_maintenance(request: Request, call_next):
        maintenance = read_maintenance(data_dir)
        blocked = maintenance.state == "frozen" and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }
        if maintenance.state == "drain" and _starts_new_work(request.method, request.url.path):
            blocked = True
        if blocked:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "maintenance_mode",
                        "message": f"AXIOM is in {maintenance.state} maintenance mode",
                    },
                    "maintenance": {
                        "state": maintenance.state,
                        "reason": maintenance.reason,
                        "updated_at": maintenance.updated_at,
                    },
                },
                headers={"Retry-After": "5"},
            )
        response = await call_next(request)
        response.headers["X-AXIOM-Maintenance"] = maintenance.state
        return response

    static_dir = Path(__file__).with_name("static")
    index_file = static_dir / "index.html"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    # 启动时一次性构建未登录/已登录两份 HTML 缓存
    if index_file.is_file():
        app.state.index_authed, app.state.index_unauth = _build_index_variants(index_file)
    else:
        app.state.index_authed = b""
        app.state.index_unauth = b""

    def _serve_index(request: Request) -> HTMLResponse:
        """按登录态返回 index.html：未登录精简版（仅 login.js）or 已登录完整版。"""
        # 鉴权关闭模式（AXIOM_AUTH_DISABLED=1）直接返回已登录版，方便本地开发
        if os.environ.get("AXIOM_AUTH_DISABLED") == "1":
            return HTMLResponse(content=app.state.index_authed, status_code=200)
        token = _extract_user_token(request)
        container: Container = request.app.state.container
        authed = False
        if token:
            if container.auth.verify_token(token):
                authed = True
            elif request.app.state.service_token and hmac.compare_digest(
                token, request.app.state.service_token
            ):
                authed = True
        body = app.state.index_authed if authed else app.state.index_unauth
        return HTMLResponse(content=body, status_code=200)

    @app.exception_handler(DomainError)
    async def handle_domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health", tags=["system"])
    def health() -> dict:
        maintenance = read_maintenance(data_dir)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1")).scalar_one()
            migration = conn.execute(
                text(f'SELECT version_num FROM "{AXIOM_SCHEMA}".alembic_version')
            ).scalar_one()
        pool = engine.pool
        pool_status = {
            "size": getattr(pool, "size", lambda: 0)(),
            "checked_out": getattr(pool, "checkedout", lambda: 0)(),
            "overflow": max(0, getattr(pool, "overflow", lambda: 0)()),
        }
        return {
            "status": "ok",
            "api": "v1",
            "version": __version__,
            "database": {
                "backend": engine.dialect.name,
                "migration": migration,
                "pool": pool_status,
            },
            "maintenance": {
                "state": maintenance.state,
                "reason": maintenance.reason,
                "updated_at": maintenance.updated_at,
            },
        }

    if index_file.is_file():

        @app.get("/", include_in_schema=False)
        def frontend(request: Request) -> HTMLResponse:
            return _serve_index(request)

        @app.get("/{frontend_path:path}", include_in_schema=False)
        def frontend_fallback(request: Request, frontend_path: str) -> Response:
            if frontend_path == "favicon.svg":
                return FileResponse(static_dir / "favicon.svg")
            # 公网安全：API 文档路径显式 404（FastAPI 自动文档已关闭，这里兜底）
            if frontend_path in {"docs", "redoc", "openapi.json"}:
                raise HTTPException(status_code=404, detail="Not Found")
            if frontend_path == "api" or frontend_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            return _serve_index(request)

    return app


def _starts_new_work(method: str, path: str) -> bool:
    if method != "POST":
        return False
    if path.endswith("/claim"):
        return True
    if path in {
        "/api/v1/tasks",
        "/api/v1/dispatch/reason-runs/reconcile",
        "/api/v1/dispatch/task-deadlines/reconcile",
    }:
        return True
    if path.endswith(("/reason-runs/trigger", "/continuations", "/resume")):
        return True
    if path.endswith("/reviews") and "/vulnerabilities/" in path:
        return True
    return path.startswith("/api/v1/tasks/") and path.endswith("/vulnerabilities")
