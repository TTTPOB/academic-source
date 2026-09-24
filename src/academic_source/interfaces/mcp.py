"""Official MCP tools backed by the same application as HTTP."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager, nullcontext, redirect_stdout
from functools import partial
from typing import TYPE_CHECKING, Any

from anyio import to_thread
from mcp.server.fastmcp import FastMCP

from academic_source.domain import AcquisitionRequest
from academic_source.interfaces.presentation import job_data

if TYPE_CHECKING:
    from academic_source.services.application import Application


def create_mcp(application: Application, *, stdio: bool = False) -> FastMCP:
    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        # The SDK binds its protocol output before starting this lifespan.
        # Provider/converter print calls must not become JSON-RPC stdout.
        with redirect_stdout(sys.stderr) if stdio else nullcontext():
            try:
                yield {}
            finally:
                if stdio:
                    await to_thread.run_sync(application.close)

    # The SDK route stays /mcp; mounting its ASGI app at / avoids /mcp/mcp.
    mcp = FastMCP(
        "academic-source",
        instructions=(
            "Acquire returns a persistent job. Poll job_status until it finishes. "
            "Files are artifacts: retrieve download_url through HTTP using the server base URL. "
            "For paper lists use inline text or upload through POST /api/v1/uploads, then pass upload_id. "
            "Client-local filesystem paths are not remote inputs."
        ),
        host="0.0.0.0",
        streamable_http_path="/mcp",
        json_response=True,
        lifespan=lifespan,
    )

    @mcp.tool()
    async def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search for scholarly publications."""
        return await to_thread.run_sync(partial(application.search, query, limit))

    @mcp.tool()
    async def resolve(identifier: str) -> dict[str, Any]:
        """Resolve a DOI, title, or other paper identifier."""
        return await to_thread.run_sync(partial(application.resolve, identifier))

    @mcp.tool()
    async def parse_list(
        upload_id: str | None = None, text: str | None = None
    ) -> list[dict[str, Any]]:
        """Parse a previously uploaded list or inline text."""
        return await to_thread.run_sync(
            partial(application.parse_list, upload_id=upload_id, text=text)
        )

    @mcp.tool()
    async def acquire(
        request: AcquisitionRequest, wait_seconds: float = 2.0
    ) -> dict[str, Any]:
        """Submit one acquisition job and briefly wait for completion."""

        def submit_and_wait() -> dict[str, Any]:
            job = application.submit(request)
            return job_data(
                application.wait(job.id, timeout=max(0.0, min(wait_seconds, 10.0)))
            )

        return await to_thread.run_sync(submit_and_wait)

    @mcp.tool()
    async def job_status(job_id: str) -> dict[str, Any]:
        """Inspect a previously submitted job and its downloadable artifacts."""
        job = await to_thread.run_sync(partial(application.job, job_id))
        return job_data(job)

    return mcp
