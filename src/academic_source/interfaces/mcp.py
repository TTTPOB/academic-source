"""Official MCP tools backed by the same application as HTTP."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager, nullcontext, redirect_stdout
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from anyio import to_thread
from mcp.server.fastmcp import Context, FastMCP

from academic_source.domain import AcquisitionRequest
from academic_source.interfaces.presentation import DownloadFactory, job_data

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
    instructions = (
        "Acquire returns a persistent job. Poll job_status until it finishes. "
        "For paper lists use inline text or a previously uploaded upload_id. "
    )
    if stdio:
        instructions += (
            "Use export_artifacts to copy registered files to output_dir on the "
            "stdio server's machine (or a mounted directory). Paths refer to that machine."
        )
    else:
        instructions += (
            "GET the absolute download_url directly without authentication headers. "
            "Each link allows one full-file GET for at most six hours. "
            "Interrupted downloads consume the link; call job_status for another. "
            "HEAD and Range downloads are not supported. "
            "Upload lists through POST /api/v1/uploads; client-local paths are not remote inputs."
        )
    mcp = FastMCP(
        "academic-source",
        instructions=instructions,
        host="0.0.0.0",
        streamable_http_path="/mcp",
        json_response=True,
        # No tool keeps per-connection state: every argument is explicit and
        # jobs live in the shared store by id. A stateless transport therefore
        # costs nothing, and a client survives a server restart instead of
        # failing with "Session not found" until it re-initializes.
        stateless_http=True,
        lifespan=lifespan,
    )

    def downloads(ctx: Context) -> DownloadFactory | None:
        if stdio:
            return None
        base_url = application.settings.public_base_url
        if not base_url:
            request = ctx.request_context.request
            if request is None:
                raise ValueError(
                    "HTTP download links require an HTTP request or public_base_url"
                )
            base_url = str(request.base_url)
        return partial(application.downloads.issue, base_url=base_url)

    @mcp.tool()
    async def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search OpenAlex for papers, excluding explicit non-paper types and retracted works."""
        return await to_thread.run_sync(partial(application.search, query, limit))

    @mcp.tool()
    async def get_metadata(
        id_type: Literal["doi", "arxiv"],
        doi: str | None = None,
        arxiv_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch bibliographic metadata for a DOI or arXiv ID.

        Sources: Crossref for DOIs; arXiv for arXiv records.
        """
        return await to_thread.run_sync(
            partial(application.get_metadata, id_type, doi=doi, arxiv_id=arxiv_id)
        )

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
        request: AcquisitionRequest, ctx: Context, wait_seconds: float = 2.0
    ) -> dict[str, Any]:
        """Submit one acquisition job and briefly wait for completion."""
        factory = downloads(ctx)

        def submit_and_wait() -> dict[str, Any]:
            job = application.submit(request)
            return job_data(
                application.wait(job.id, timeout=max(0.0, min(wait_seconds, 10.0))),
                downloadable=not stdio,
                download_factory=factory,
            )

        return await to_thread.run_sync(submit_and_wait)

    @mcp.tool()
    async def job_status(job_id: str, ctx: Context) -> dict[str, Any]:
        """Inspect a previously submitted job and its artifacts."""
        job = await to_thread.run_sync(partial(application.job, job_id))
        return job_data(job, downloadable=not stdio, download_factory=downloads(ctx))

    if stdio:

        @mcp.tool()
        async def export_artifacts(
            artifact_ids: list[str], output_dir: str
        ) -> list[dict[str, Any]]:
            """Copy registered artifacts to a directory on this stdio server's machine."""
            return await to_thread.run_sync(
                partial(application.export_artifacts, artifact_ids, output_dir)
            )

    return mcp
