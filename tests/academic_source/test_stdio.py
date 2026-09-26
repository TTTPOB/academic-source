"""A real stdio subprocess must keep provider prints out of JSON-RPC stdout."""

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.academic_source.helpers import paper_bytes

SCRIPT = """
import sys
from pathlib import Path
from academic_source.interfaces.mcp import create_mcp
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource

class NoisySource(RecordingSource):
    def __init__(self):
        super().__init__()
        self.content = Path(sys.argv[2]).read_bytes()

    def acquire(self, *args):
        print("PROVIDER_DIAGNOSTIC")
        return super().acquire(*args)

application = Application(Settings(data_dir=Path(sys.argv[1])), source=NoisySource())
try:
    create_mcp(application, stdio=True).run(transport="stdio")
finally:
    application.close()
"""


def test_stdio_acquisition_routes_provider_prints_to_stderr(tmp_path):
    root = Path(__file__).resolve().parents[2]
    original = paper_bytes()
    fixture = tmp_path / "original.pdf"
    fixture.write_bytes(original)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", SCRIPT, str(tmp_path / "data"), str(fixture)],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root))),
        },
    )
    log = tmp_path / "stderr.log"

    async def exchange():
        with log.open("w", encoding="utf-8") as stderr:
            async with (
                stdio_client(parameters, errlog=stderr) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                result = await session.call_tool(
                    "acquire",
                    {
                        "request": {"identifiers": ["10.1234/example"]},
                        "wait_seconds": 5,
                    },
                )
                assert not result.isError
                assert result.structuredContent["status"] == "succeeded"
                artifact = result.structuredContent["artifacts"][0]
                assert artifact["kind"] == "pdf"
                assert "download_url" not in artifact
                status = await session.call_tool(
                    "job_status", {"job_id": result.structuredContent["id"]}
                )
                assert (
                    "download_url"
                    not in status.structuredContent["results"][0]["artifacts"][0]
                )
                exported = await session.call_tool(
                    "export_artifacts",
                    {
                        "artifact_ids": [artifact["id"]],
                        "output_dir": str(tmp_path / "export"),
                    },
                )
                assert not exported.isError
                item = exported.structuredContent["result"][0]
                assert item["id"] == artifact["id"]
                assert Path(item["path"]).read_bytes() == original
                assert item["size"] == len(original)

    asyncio.run(exchange())
    assert "PROVIDER_DIAGNOSTIC" in log.read_text(encoding="utf-8")
