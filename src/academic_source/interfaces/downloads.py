"""Anonymous single-file capability GETs and their HTTP privacy boundaries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from starlette._utils import get_route_path
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from academic_source.services.application import Application


def _private_response_headers(scope: Scope) -> dict[str, str]:
    path = get_route_path(scope)
    if path == "/downloads" or path.startswith("/downloads/"):
        return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if path.rstrip("/") == "/mcp":
        return {"Cache-Control": "no-store"}
    return {}


async def download_server_error_handler(request: Request, exc: Exception) -> Response:
    """Keep framework-generated 500 responses private without swallowing errors."""
    return PlainTextResponse(
        "Internal Server Error",
        status_code=500,
        headers=_private_response_headers(request.scope),
    )


class DownloadResponseHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        private_headers = _private_response_headers(scope)

        async def send_private(message: Message) -> None:
            if private_headers and message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(private_headers)
            await send(message)

        await self.app(scope, receive, send_private)


class _DownloadLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's access record args are (client, method, target, version, status).
        if isinstance(record.args, tuple) and len(record.args) == 5:
            target = str(record.args[2])
            prefix, marker, _ = target.partition("/downloads/")
            if marker:
                record.args = (
                    *record.args[:2],
                    f"{prefix}/downloads/[redacted]",
                    *record.args[3:],
                )
        return True


_DOWNLOAD_LOG_FILTER = _DownloadLogFilter()


def create_download_router(application: Application) -> APIRouter:
    logging.getLogger("uvicorn.access").addFilter(_DOWNLOAD_LOG_FILTER)
    router = APIRouter()

    # Explicit rejects must precede the catch-all MCP mount, which would otherwise
    # turn Starlette's partial (method-only) route match into a 404.
    @router.api_route(
        "/downloads/{token}",
        methods=[
            "HEAD",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
            "TRACE",
            "CONNECT",
        ],
        include_in_schema=False,
    )
    def reject_method() -> None:
        raise HTTPException(
            status_code=405, detail="Method Not Allowed", headers={"Allow": "GET"}
        )

    @router.get("/downloads/{token}")
    def download(token: str, request: Request) -> FileResponse:
        # FileResponse supports Range by default; reject it before consuming a ticket.
        if "range" in request.headers:
            raise HTTPException(
                status_code=400, detail="Range requests are not supported"
            )
        try:
            artifact, path = application.downloads.claim(token)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Download not found") from exc
        return FileResponse(
            path,
            media_type=artifact.media_type,
            filename=artifact.filename,
            headers={
                "Accept-Ranges": "none",
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    return router
