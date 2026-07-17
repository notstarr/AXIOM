from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from axiom import __version__
from axiom.server import db
from axiom.server.routers import assets, crawler, diagnostics, export, facts, files, hints, intents, projects, settings, tools, mcp, tool_hooks

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    yield


app = FastAPI(
    title="AXIOM",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(crawler.router)
app.include_router(tools.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(facts.router)
app.include_router(export.router)
app.include_router(files.router)
app.include_router(assets.router)
app.include_router(mcp.router)
app.include_router(diagnostics.router)
app.include_router(tool_hooks.router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/legacy", include_in_schema=False)
def legacy():
    return FileResponse(STATIC_DIR / "index-legacy.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
