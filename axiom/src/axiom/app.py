from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine

from axiom import __version__
from axiom.api.router import router
from axiom.composition import Container
from axiom.shared.errors import DomainError


def create_app(*, engine: Engine, data_dir: Path) -> FastAPI:
    app = FastAPI(
        title="AXIOM API",
        version=__version__,
        description="Modular Task/Fact/Intent orchestration backend",
    )
    app.state.container = Container.build(engine, data_dir=data_dir)
    app.include_router(router)

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
    def health() -> dict[str, str]:
        return {"status": "ok", "api": "v1", "version": __version__}

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
