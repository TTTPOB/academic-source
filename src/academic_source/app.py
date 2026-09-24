"""Compose HTTP, MCP, and application resource lifecycles."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from anyio import to_thread
from fastapi import FastAPI

from academic_source.interfaces.api import create_router
from academic_source.interfaces.mcp import create_mcp
from academic_source.interfaces.web import create_web_router
from academic_source.settings import Settings

if TYPE_CHECKING:
    from academic_source.services.application import Application


def create_app(
    settings: Settings | None = None, application: Application | None = None
) -> FastAPI:
    if application is None:
        from academic_source.services.application import Application

        application = Application(settings or Settings.load())
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
    app.state.application = service
    app.state.mcp = mcp
    app.include_router(create_router(service))
    app.include_router(create_web_router())
    app.mount("/", transport)
    return app
