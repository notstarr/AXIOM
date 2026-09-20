"""LLM 专用网关：让 Worker 的 LLM 请求经 serve 转发，代理只作用于这一跳。

背景：Pi 子进程直接访问 LLM provider（PI_BASE_URL）。若在 Pi 进程注入
HTTPS_PROXY，skill 子进程（curl/Go 系工具）会继承代理，渗透目标流量也会
被卷入。本网关把"LLM 走代理"变成结构保证：

    Pi ──(直连 localhost)──→ serve /llm-gateway ──(可选代理)──→ provider

- 上游 worker 的真实 base_url / api_key 只存在于 serve 侧；
- Pi 的 models.json 由 dispatcher.pi._write_models_file 改写指向本网关，
  apiKey 使用 service token（走统一鉴权中间件）；
- 代理按后端开关决定：PiWorkerConfig.llm_proxy 开且全局 mode==http 时转发走代理，
  其余情况直连；统一经 pi_config.llm_proxy_for 解析。"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.workers.pi_config import (
    PiWorkerConfig,
    is_anthropic_messages,
    llm_proxy_for,
    provider_auth_headers,
    upstream_request_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-gateway", tags=["llm-gateway"])

_HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailers",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
    "accept-encoding",
}

_STREAM_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]




@router.api_route("/{worker_name}/{path:path}", methods=_STREAM_METHODS)
async def llm_gateway(
    worker_name: str,
    path: str,
    request: Request,
    container: Container = Depends(get_container),
):
    worker = container.workers.get(worker_name)
    config = PiWorkerConfig.model_validate(worker.config)
    if not config.base_url:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "gateway_no_base_url", "message": f"Worker {worker_name} 未配置 base_url"}},
        )

    upstream_url = upstream_request_url(config.provider_api, config.base_url, path)
    if request.url.query:
        upstream_url += "?" + request.url.query

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
        and k.lower() not in {"authorization", "x-api-key"}
    }
    auth_headers = provider_auth_headers(config.provider_api, config.api_key)
    if is_anthropic_messages(config.provider_api):
        # Keep a caller-supplied Anthropic version/beta header, but never let
        # the Pi service token or an incoming API key reach the upstream.
        auth_headers.pop("anthropic-version", None)
        headers.setdefault("anthropic-version", "2023-06-01")
    headers.update(auth_headers)

    body = await request.body()
    proxy_url = llm_proxy_for(container.settings.get().config, config)
    client = httpx.AsyncClient(
        proxy=proxy_url,
        timeout=httpx.Timeout(connect=15.0, write=60.0, read=None, pool=None),
    )
    upstream_request = client.build_request(
        request.method,
        upstream_url,
        headers=headers,
        content=body,
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("llm-gateway upstream %s failed: %s", worker_name, exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"code": "gateway_upstream_error", "message": f"上游请求失败：{exc}"}},
        )

    response_headers = {
        k: v for k, v in upstream_response.headers.items() if k.lower() not in _HOP_BY_HOP
    }

    async def stream_upstream():
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()
            await client.aclose()

    if proxy_url:
        response_headers.setdefault("x-axiom-llm-proxy", "1")
    return StreamingResponse(
        stream_upstream(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
