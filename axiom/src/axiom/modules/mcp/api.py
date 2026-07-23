from __future__ import annotations

from fastapi import APIRouter, Depends, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.mcp.schemas import (
    MCPImportRequest,
    MCPServer,
    MCPServerInput,
    MCPTool,
    MCPToolToggle,
    MCPWorkerBinding,
    MCPWorkerBindingView,
)
from axiom.modules.mcp.service import parse_mcp_config


router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/servers", response_model=list[MCPServer])
def list_mcp_servers(container: Container = Depends(get_container)) -> list[MCPServer]:
    return container.mcp.list()


@router.post("/servers", response_model=MCPServer, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    body: MCPServerInput,
    container: Container = Depends(get_container),
) -> MCPServer:
    created = container.mcp.create(body)
    return await container.mcp.discover(created.id) if not created.disabled else created


@router.put("/servers/{server_id}", response_model=MCPServer)
def update_mcp_server(
    server_id: str,
    body: MCPServerInput,
    container: Container = Depends(get_container),
) -> MCPServer:
    return container.mcp.update(server_id, body)


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(
    server_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.mcp.delete(server_id)


@router.post("/import", response_model=list[MCPServer], status_code=status.HTTP_201_CREATED)
async def import_mcp_servers(
    body: MCPImportRequest,
    container: Container = Depends(get_container),
) -> list[MCPServer]:
    _format, entries = parse_mcp_config(body.content, body.format)
    if body.replace:
        for existing in container.mcp.list():
            container.mcp.delete(existing.id)
    result: list[MCPServer] = []
    for entry in entries:
        created = container.mcp.create(entry)
        if body.discover and not created.disabled:
            created = await container.mcp.discover(created.id)
        result.append(created)
    return result


@router.post("/servers/{server_id}/discover", response_model=MCPServer)
async def discover_mcp_tools(
    server_id: str,
    container: Container = Depends(get_container),
) -> MCPServer:
    return await container.mcp.discover(server_id)


@router.put("/servers/{server_id}/tools", response_model=list[MCPTool])
def replace_mcp_tools(
    server_id: str,
    body: list[dict],
    container: Container = Depends(get_container),
) -> list[MCPTool]:
    return container.mcp.replace_tools(server_id, body)


@router.patch("/servers/{server_id}/tools/{tool_id}", response_model=MCPTool)
def toggle_mcp_tool(
    server_id: str,
    tool_id: str,
    body: MCPToolToggle,
    container: Container = Depends(get_container),
) -> MCPTool:
    return container.mcp.set_tool_enabled(server_id, tool_id, body.enabled)


@router.get("/workers/{worker_name}", response_model=MCPWorkerBindingView)
def get_mcp_worker_binding(
    worker_name: str,
    container: Container = Depends(get_container),
) -> MCPWorkerBindingView:
    return container.mcp.worker_binding(worker_name)


@router.get("/workers", response_model=list[MCPWorkerBindingView])
def list_mcp_worker_bindings(
    container: Container = Depends(get_container),
) -> list[MCPWorkerBindingView]:
    return container.mcp.list_worker_bindings()


@router.put("/workers/{worker_name}", response_model=MCPWorkerBindingView)
def save_mcp_worker_binding(
    worker_name: str,
    body: MCPWorkerBinding,
    container: Container = Depends(get_container),
) -> MCPWorkerBindingView:
    return container.mcp.save_worker_binding(worker_name, body)
