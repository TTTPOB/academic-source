"""Compose HTTP, MCP, and application resource lifecycles."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from anyio import to_thread
from fastapi import FastAPI

from academic_source.interfaces.api import create_router
from academic_source.interfaces.auth import BearerAuthMiddleware
from academic_source.interfaces.downloads import (
    DownloadResponseHeadersMiddleware,
    create_download_router,
    download_server_error_handler,
)
from academic_source.interfaces.mcp import create_mcp
from academic_source.interfaces.web import create_web_router
from academic_source.settings import Settings

if TYPE_CHECKING:
    from academic_source.services.application import Application


def create_app(
    settings: Settings | None = None, application: Application | None = None
) -> FastAPI:
    settings = settings or (
        application.settings if application is not None else Settings.load()
    )
    if settings.auth_mode == "required" and not settings.api_keys:
        raise ValueError(
            "HTTP authentication requires at least one API key; "
            "set ACADEMIC_SOURCE_API_KEYS or explicitly select auth_mode=off"
        )
    logging.getLogger(__name__).info("HTTP authentication mode: %s", settings.auth_mode)
    if application is None:
        from academic_source.services.application import Application

        application = Application(settings)
    service = application
    mcp = create_mcp(service)
    transport = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await to_thread.run_sync(service.close)

    app = FastAPI(title="academic-source", lifespan=lifespan)
    app.add_exception_handler(500, download_server_error_handler)
    app.state.application = service
    app.state.mcp = mcp
    app.include_router(create_router(service))
    app.include_router(create_web_router())
    app.include_router(create_download_router(service))
    if settings.auth_mode == "required":
        app.add_middleware(BearerAuthMiddleware, api_keys=settings.api_keys)
    app.add_middleware(DownloadResponseHeadersMiddleware)
    app.mount("/", transport)
    return app
