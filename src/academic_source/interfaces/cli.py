"""Local and HTTP clients for the shared acquisition service."""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from academic_source.domain import AcquisitionRequest
from academic_source.interfaces.presentation import job_data
from academic_source.settings import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="academic-source")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve HTTP API, MCP and web UI together")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("mcp", help="Run an MCP stdio server")
    get = commands.add_parser("get", help="Get one paper")
    get.add_argument("identifier")
    batch = commands.add_parser("batch", help="Acquire a local literature list")
    batch.add_argument("file", type=Path)
    for command in (get, batch):
        command.add_argument("--server", help="HTTP server URL; omit for local mode")
        command.add_argument(
            "--output", type=Path, default=Path("."), help="Local output directory"
        )
        command.add_argument("--policy", default="fastest")
        command.add_argument("--markdown", action="store_true")
        command.add_argument("--supplementary", action="store_true")
        command.add_argument("--bibtex", action="store_true")
    return parser


def _save_path(output: Path, item: dict[str, Any]) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    # An artifact's display filename never becomes an arbitrary output path.
    return output / f"{item['id']}-{Path(item['filename']).name}"


def _remote(args: argparse.Namespace) -> dict[str, Any]:
    with httpx.Client(
        base_url=args.server.rstrip("/") + "/", timeout=30.0, follow_redirects=True
    ) as client:
        if args.command == "batch":
            with args.file.open("rb") as stream:
                response = client.post(
                    "api/v1/uploads", files={"file": (args.file.name, stream)}
                )
            response.raise_for_status()
            request = {"upload_id": response.json()["id"], "policy": args.policy}
        else:
            request = {"identifiers": [args.identifier], "policy": args.policy}
        request.update(
            markdown=args.markdown, supplementary=args.supplementary, bibtex=args.bibtex
        )
        response = client.post("api/v1/acquisitions", json=request)
        response.raise_for_status()
        job = response.json()
        while job["status"] in ("queued", "running"):
            time.sleep(0.5)
            response = client.get(f"api/v1/jobs/{job['id']}")
            response.raise_for_status()
            job = response.json()
        for item in job["artifacts"]:
            path = _save_path(args.output, item)
            partial = path.with_suffix(path.suffix + ".part")
            try:
                with client.stream("GET", item["download_url"].lstrip("/")) as response:
                    response.raise_for_status()
                    with partial.open("wb") as destination:
                        for chunk in response.iter_bytes():
                            destination.write(chunk)
                partial.replace(path)
            finally:
                partial.unlink(missing_ok=True)
        return job


def _local(args: argparse.Namespace) -> dict[str, Any]:
    from academic_source.services.application import Application

    application = Application(Settings.load())
    try:
        if args.command == "batch":
            with args.file.open("rb") as stream:
                upload = application.store.put_upload(args.file.name, stream)
            request = AcquisitionRequest(upload_id=upload.id, policy=args.policy)
        else:
            request = AcquisitionRequest(
                identifiers=[args.identifier], policy=args.policy
            )
        request.markdown = args.markdown
        request.supplementary = args.supplementary
        request.bibtex = args.bibtex
        job = application.submit(request)
        while job.status in ("queued", "running"):
            job = application.wait(job.id, timeout=0.5)
        for item in job.artifacts:
            destination = _save_path(args.output, item.model_dump(mode="json"))
            shutil.copyfile(application.store.artifact_path(item.id), destination)
        return job_data(job)
    finally:
        application.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        from academic_source.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    if args.command == "mcp":
        from academic_source.interfaces.mcp import create_mcp
        from academic_source.services.application import Application

        application = Application(Settings.load())
        try:
            create_mcp(application, stdio=True).run(transport="stdio")
        finally:
            application.close()
        return 0
    try:
        job = _remote(args) if args.server else _local(args)
    except (
        OSError,
        httpx.HTTPError,
        httpx.InvalidURL,
        ValueError,
        RuntimeError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(job, ensure_ascii=True, indent=2))
    return 0 if job["status"] == "succeeded" else 1
