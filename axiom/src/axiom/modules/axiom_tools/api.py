from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.axiom_tools.schemas import (
    AxiomTool,
    AxiomToolAuditCommand,
    AxiomToolBindingSelection,
    AxiomToolScanResult,
    UpdateAxiomTool,
    WorkerAxiomToolBindings,
    WorkerAxiomToolBindingsView,
)


router = APIRouter(tags=["axiom-tools"])


@router.get("/axiom-tools", response_model=list[AxiomTool])
def list_axiom_tools(container: Container = Depends(get_container)) -> list[AxiomTool]:
    return container.axiom_tools.list()


@router.post("/axiom-tools/scan", response_model=AxiomToolScanResult)
def scan_axiom_tools(container: Container = Depends(get_container)) -> AxiomToolScanResult:
    return container.axiom_tools.scan()


@router.get("/axiom-tools/{key}", response_model=AxiomTool)
def get_axiom_tool(key: str, container: Container = Depends(get_container)) -> AxiomTool:
    return container.axiom_tools.get(key)


@router.patch("/axiom-tools/{key}", response_model=AxiomTool)
def update_axiom_tool(
    key: str,
    body: UpdateAxiomTool,
    container: Container = Depends(get_container),
) -> AxiomTool:
    return container.axiom_tools.update(key, body)


@router.put("/axiom-tools/{key}/bindings", response_model=AxiomTool)
def save_axiom_tool_bindings(
    key: str,
    body: AxiomToolBindingSelection,
    container: Container = Depends(get_container),
) -> AxiomTool:
    return container.axiom_tools.save_tool_bindings(key, body)


@router.get(
    "/workers/{name}/axiom-tools",
    response_model=WorkerAxiomToolBindingsView,
)
def get_worker_axiom_tools(
    name: str,
    container: Container = Depends(get_container),
) -> WorkerAxiomToolBindingsView:
    return container.axiom_tools.worker_bindings(name)


@router.put(
    "/workers/{name}/axiom-tools",
    response_model=WorkerAxiomToolBindingsView,
)
def save_worker_axiom_tools(
    name: str,
    body: WorkerAxiomToolBindings,
    container: Container = Depends(get_container),
) -> WorkerAxiomToolBindingsView:
    return container.axiom_tools.save_worker_bindings(name, body)


@router.post("/internal/axiom-tools/events", status_code=204)
def record_axiom_tool_event(
    body: AxiomToolAuditCommand,
    container: Container = Depends(get_container),
) -> None:
    container.axiom_tools.record_audit(body)
