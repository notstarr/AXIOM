from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import PlainTextResponse

from axiom.api.dependencies import get_container
from axiom.composition import Container


router = APIRouter(prefix="/tasks", tags=["exports"])


@router.get("/{task_id}/export")
def export_task(
    task_id: str,
    format: str = Query(default="yaml"),
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    if format == "yaml":
        content = container.exports.overview_yaml(task_id)
    elif format == "json":
        content = container.exports.overview_json(task_id)
    elif format == "timeline":
        content = container.exports.timeline(task_id)
    else:
        from axiom.shared.errors import ConflictError

        raise ConflictError(f"Unsupported export format: {format}")
    return PlainTextResponse(content)


@router.get("/{task_id}/vulnerabilities-export")
def export_task_vulnerabilities(
    task_id: str,
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    return PlainTextResponse(container.exports.vulnerabilities_markdown(task_id))


def _attachment(payload: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{task_id}/vulnerabilities/{vulnerability_id}/export")
def export_vulnerability(
    task_id: str,
    vulnerability_id: str,
    format: str = Query(default="auto"),
    images: str = Query(default="include"),
    container: Container = Depends(get_container),
) -> Response:
    """导出一份报告：md，有图时是 md + images 的压缩包。

    ``auto`` 按有没有图决定包形；显式 ``md`` / ``markdown`` 只出正文（图片路径仍在，
    需要调用方自己取图），``zip`` 强制打包（没有图时就是一个只含 md 的包）。

    ``images=omit`` 连图片引用一起去掉。这是复制到剪贴板用的：读者手里没有那个文件，
    留一条指不过去的路径不如不留。
    """
    if format in {"md", "markdown"}:
        payload = container.exports.vulnerability_markdown(
            task_id, vulnerability_id, with_images=images != "omit"
        )
        return _attachment(
            payload.encode("utf-8"), f"{task_id}_{vulnerability_id}_report.md", "text/markdown"
        )
    payload, filename, media_type = container.exports.export_vulnerability_report(
        task_id, vulnerability_id
    )
    if format == "zip" and media_type != "application/zip":
        import io as _io
        import zipfile as _zipfile

        buffer = _io.BytesIO()
        with _zipfile.ZipFile(buffer, "w", _zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.md", payload)
        return _attachment(buffer.getvalue(), filename.replace(".md", ".zip"), "application/zip")
    return _attachment(payload, filename, media_type)


@router.get("/{task_id}/red-team/results/{result_id}/export")
def export_result(
    task_id: str,
    result_id: str,
    format: str = Query(default="auto"),
    container: Container = Depends(get_container),
) -> Response:
    if format in {"md", "markdown"}:
        payload = container.exports.result_markdown(task_id, result_id)
        return _attachment(
            payload.encode("utf-8"), f"{task_id}_{result_id}_report.md", "text/markdown"
        )
    payload, filename, media_type = container.exports.export_result_report(task_id, result_id)
    return _attachment(payload, filename, media_type)


@router.get("/{task_id}/report")
def export_task_report(
    task_id: str,
    format: str = Query(default="auto"),
    scope: str = Query(default="verified"),
    container: Container = Depends(get_container),
) -> Response:
    """一个任务一整份报告。

    包形与单条导出完全一致：有图是 md + images 的压缩包，没图就是一份 md。
    ``scope=verified``（默认）只收已验证的漏洞，``scope=all`` 把待验证也收进正文；
    忽略/重复永远只进附录，不进执行摘要。
    """
    if scope not in {"verified", "all"}:
        from axiom.shared.errors import ConflictError

        raise ConflictError(f"Unsupported report scope: {scope}")
    if format in {"md", "markdown"}:
        payload = container.exports.task_report_markdown(task_id, scope=scope)
        return _attachment(
            payload.encode("utf-8"), f"{task_id}_report.md", "text/markdown"
        )
    payload, filename, media_type = container.exports.export_task_report(
        task_id, scope=scope
    )
    if format == "zip" and media_type != "application/zip":
        import io as _io
        import zipfile as _zipfile

        buffer = _io.BytesIO()
        with _zipfile.ZipFile(buffer, "w", _zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.md", payload)
        return _attachment(
            buffer.getvalue(), filename.replace(".md", ".zip"), "application/zip"
        )
    return _attachment(payload, filename, media_type)
