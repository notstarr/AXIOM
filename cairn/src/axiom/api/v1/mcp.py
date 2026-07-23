from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from axiom.api.v1.dependencies import get_v2_engine
from axiom.modules.mcp.schemas import MCPImportRequest, MCPServer, MCPServerInput, MCPTool, MCPToolToggle, MCPWorkerBinding
from axiom.modules.mcp.service import MCPService, parse_mcp_config


router = APIRouter(prefix="/mcp", tags=["v1-mcp"])


def service(engine=Depends(get_v2_engine)) -> MCPService:
    return MCPService(engine)


@router.get("/servers", response_model=list[MCPServer])
def list_servers(mcp: MCPService = Depends(service)) -> list[MCPServer]:
    return mcp.list()


@router.post("/servers", response_model=MCPServer, status_code=201)
async def create_server(body: MCPServerInput, mcp: MCPService = Depends(service)) -> MCPServer:
    try:
        created = mcp.create(body)
        return await mcp.discover(created.id) if not created.disabled else created
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/servers/{server_id}", response_model=MCPServer)
def update_server(server_id: str, body: MCPServerInput, mcp: MCPService = Depends(service)) -> MCPServer:
    try:
        return mcp.update(server_id, body)
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 409, str(exc)) from exc


@router.delete("/servers/{server_id}", status_code=204)
def delete_server(server_id: str, mcp: MCPService = Depends(service)) -> None:
    try:
        mcp.delete(server_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/import", response_model=list[MCPServer], status_code=201)
async def import_servers(body: MCPImportRequest, mcp: MCPService = Depends(service)) -> list[MCPServer]:
    try:
        _, items = parse_mcp_config(body.content, body.format)
        if body.replace:
            for existing in mcp.list():
                mcp.delete(existing.id)
        created = [mcp.create(item) for item in items]
        if not body.discover:
            return created
        result: list[MCPServer] = []
        for item in created:
            result.append(await mcp.discover(item.id) if not item.disabled else item)
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/servers/{server_id}/tools", response_model=list[MCPTool])
def replace_tools(server_id: str, body: list[dict], mcp: MCPService = Depends(service)) -> list[MCPTool]:
    try:
        return mcp.replace_tools(server_id, body)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/servers/{server_id}/discover", response_model=MCPServer)
async def discover_tools(server_id: str, mcp: MCPService = Depends(service)) -> MCPServer:
    try:
        return await mcp.discover(server_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.patch("/servers/{server_id}/tools/{tool_id}", response_model=MCPTool)
def toggle_tool(server_id: str, tool_id: str, body: MCPToolToggle, mcp: MCPService = Depends(service)) -> MCPTool:
    try:
        return mcp.set_tool_enabled(server_id, tool_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/workers", response_model=list[dict])
def list_worker_bindings(engine=Depends(get_v2_engine)) -> list[dict]:
    from axiom.modules.workers.service import WorkerService
    workers = WorkerService(engine).list()
    mcp = MCPService(engine)
    return [mcp.worker_binding(worker.name) | {"disabled": worker.disabled, "worker_type": worker.worker_type} for worker in workers]


@router.get("/workers/{worker_name}", response_model=dict)
def get_worker_binding(worker_name: str, mcp: MCPService = Depends(service)) -> dict:
    try:
        return mcp.worker_binding(worker_name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put("/workers/{worker_name}", response_model=dict)
def save_worker_binding(worker_name: str, body: MCPWorkerBinding, mcp: MCPService = Depends(service)) -> dict:
    try:
        return mcp.save_worker_binding(worker_name, body.enabled, body.server_ids, body.allowed_tools)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
