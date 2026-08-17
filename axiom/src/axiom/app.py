from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, text

from axiom import __version__
from axiom.api.router import router
from axiom.composition import Container
from axiom.shared.database import AXIOM_SCHEMA
from axiom.shared.errors import DomainError
from axiom.shared.maintenance import read_maintenance


def create_app(*, engine: Engine, data_dir: Path) -> FastAPI:
    data_dir = data_dir.expanduser().resolve()
    app = FastAPI(
        title="AXIOM API",
        version=__version__,
        description="Modular Task/Fact/Intent orchestration backend",
    )
    initial_maintenance = read_maintenance(data_dir)
    app.state.container = Container.build(
        engine,
        data_dir=data_dir,
        allow_startup_writes=initial_maintenance.state != "frozen",
    )
    app.include_router(router)

    @app.middleware("http")
    async def enforce_auth(request: Request, call_next):
        # 测试/开发模式开关：AXIOM_AUTH_DISABLED=1 时跳过鉴权拦截
        if os.environ.get("AXIOM_AUTH_DISABLED") == "1":
            return await call_next(request)
        path = request.url.path
        # 豁免路径：健康检查、认证接口、静态前端资源与 OpenAPI 文档
        if (
            path == "/health"
            or path.startswith("/api/v1/auth/login")
            or path.startswith("/api/v1/auth/status")
            or path.startswith("/static")
            or path in {"/", "/favicon.svg", "/docs", "/redoc", "/openapi.json"}
            or not path.startswith("/api/")
        ):
            return await call_next(request)

        # 检查 Authorization header 或 Cookie
        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
        elif "axiom_token" in request.cookies:
            token = request.cookies.get("axiom_token", "").strip()

        container: Container = request.app.state.container
        if not token or not container.auth.verify_token(token):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "unauthorized",
                        "message": "未登录或登录凭据已过期，请重新登录",
                    }
                },
            )

        return await call_next(request)

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
        def frontend() -> FileResponse:
            return FileResponse(index_file)

        @app.get("/{frontend_path:path}", include_in_schema=False)
        def frontend_fallback(frontend_path: str) -> FileResponse:
            if frontend_path == "favicon.svg":
                return FileResponse(static_dir / "favicon.svg")
            if frontend_path == "api" or frontend_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            return FileResponse(index_file)

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
