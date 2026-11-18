"""Command line: a client for a running server, plus the server's own commands.

The default installation has only httpx. Everything that needs the ``server``
extra is imported inside the command that uses it.
"""

import argparse
import json
import logging
import os
import shutil
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import httpx

from academic_source import client as remote
from academic_source.interfaces.signals import graceful_signals

_SERVER_EXTRA = (
    "This command runs the service itself and needs the server extra: "
    "install 'academic-source[server]'."
)
_NO_SERVER = (
    "No server given. Pass --server or set ACADEMIC_SOURCE_SERVER. "
    "To run the service in this process instead, install 'academic-source[server]'."
)


class _ServerExtraMissing(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=Path(sys.argv[0]).name or "asource")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve", help="Serve HTTP API, MCP and web UI together (server extra)"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("mcp", help="Run an MCP stdio server (server extra)")
    prune = commands.add_parser(
        "prune", help="Preview unused local files and records (server extra)"
    )
    prune.add_argument("--apply", action="store_true", help="Delete the reported items")
    get = commands.add_parser("get", help="Get one paper and save its files")
    get.add_argument("identifier")
    batch = commands.add_parser(
        "batch", help="Get every paper in a local literature list"
    )
    batch.add_argument("file", type=Path)
    for command in (get, batch):
        command.add_argument(
            "--output", type=Path, default=Path("."), help="Local output directory"
        )
        command.add_argument(
            "--no-download",
            action="store_true",
            help="Wait for the job but leave its files on the server",
        )
        command.add_argument("--policy", default="fastest")
        command.add_argument("--markdown", action="store_true")
        command.add_argument("--supplementary", action="store_true")
        command.add_argument("--bibtex", action="store_true")
    search = commands.add_parser("search", help="Search for papers by keywords")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    metadata = commands.add_parser("metadata", help="Look up one DOI or arXiv ID")
    identity = metadata.add_mutually_exclusive_group(required=True)
    identity.add_argument("--doi")
    identity.add_argument("--arxiv")
    status = commands.add_parser("status", help="Show a job on the server")
    status.add_argument("job_id")
    download = commands.add_parser(
        "download", help="Save the finished files of a job on the server"
    )
    download.add_argument("job_id")
    download.add_argument(
        "--output", type=Path, default=Path("."), help="Local output directory"
    )
    cancel = commands.add_parser("cancel", help="Cancel a job on the server")
    cancel.add_argument("job_id")
    for command in (get, batch, search, metadata, status, download, cancel):
        command.add_argument(
            "--server",
            default=os.environ.get("ACADEMIC_SOURCE_SERVER"),
            help="Server URL (default: $ACADEMIC_SOURCE_SERVER)",
        )
        command.add_argument(
            "--api-key-file",
            type=Path,
            default=os.environ.get("ACADEMIC_SOURCE_API_KEY_FILE"),
            help="File holding the server key (default: "
            "$ACADEMIC_SOURCE_API_KEY_FILE); $ACADEMIC_SOURCE_API_KEY wins",
        )
    return parser


def _application() -> Any:
    try:
        from academic_source.services.application import Application
        from academic_source.settings import Settings
    except ImportError as exc:
        raise _ServerExtraMissing from exc
    return Application(Settings.load())


def _remote(args: argparse.Namespace) -> Any:
    with remote.open_client(args.server, args.api_key_file) as client:
        if args.command == "search":
            return remote.search(client, args.query, args.limit)
        if args.command == "metadata":
            return remote.metadata(client, doi=args.doi, arxiv=args.arxiv)
        if args.command == "status":
            return remote.job(client, args.job_id)
        if args.command == "cancel":
            return remote.cancel(client, args.job_id)
        if args.command == "download":
            job = remote.job(client, args.job_id)
            remote.download(client, job, args.output)
            return job
        job = remote.submit(
            client,
            identifier=getattr(args, "identifier", None),
            list_file=getattr(args, "file", None),
            policy=args.policy,
            markdown=args.markdown,
            supplementary=args.supplementary,
            bibtex=args.bibtex,
        )
        stop: list[bool] = []
        with graceful_signals(lambda: stop.append(True), drain_notice=False):
            job = remote.wait(client, job, lambda: bool(stop))
        if not args.no_download:
            remote.download(client, job, args.output)
        return job


def _local(args: argparse.Namespace) -> Any:
    application = _application()
    try:
        if args.command == "search":
            return application.search(args.query, args.limit)
        if args.command == "metadata":
            return application.get_metadata(
                "doi" if args.doi else "arxiv", doi=args.doi, arxiv_id=args.arxiv
            )
        with graceful_signals(application.close):
            return _local_job(args, application)
    finally:
        application.close()


def _local_job(args: argparse.Namespace, application: Any) -> dict[str, Any]:
    from academic_source.domain import AcquisitionRequest
    from academic_source.interfaces.presentation import job_data

    if args.command == "batch":
        with args.file.open("rb") as stream:
            upload = application.store.put_upload(args.file.name, stream)
        request = AcquisitionRequest(upload_id=upload.id, policy=args.policy)
    else:
        request = AcquisitionRequest(identifiers=[args.identifier], policy=args.policy)
    request.markdown = args.markdown
    request.supplementary = args.supplementary
    request.bibtex = args.bibtex
    job = application.submit(request)
    while job.status in ("queued", "running"):
        job = application.wait(job.id, timeout=0.5)
    if not args.no_download:
        for item in job.artifacts:
            destination = remote.save_path(args.output, item.model_dump(mode="json"))
            shutil.copyfile(application.store.artifact_path(item.id), destination)
    return job_data(job)


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from academic_source.app import create_app
    except ImportError as exc:
        raise _ServerExtraMissing from exc

    class DrainServer(uvicorn.Server):
        # The CLI owns signals; Uvicorn otherwise replays them on exit.
        capture_signals = staticmethod(nullcontext)

    # Uvicorn configures only its own loggers; attempt timings are INFO lines.
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    service_log = logging.getLogger("academic_source")
    service_log.addHandler(handler)
    service_log.setLevel(logging.INFO)
    # A library may install a root handler; without this each line prints twice.
    service_log.propagate = False
    try:
        app = create_app()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    server = DrainServer(uvicorn.Config(app, host=args.host, port=args.port))
    with graceful_signals(lambda: setattr(server, "should_exit", True)):
        server.run()
    return 0


def _mcp() -> int:
    application = _application()
    from academic_source.interfaces.mcp import create_mcp

    def stop_stdio() -> None:
        # The SDK's stdin worker cannot exit while the client keeps stdin open.
        application.close()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    try:
        with graceful_signals(stop_stdio):
            create_mcp(application, stdio=True).run(transport="stdio")
    finally:
        application.close()
    return 0


def _prune(args: argparse.Namespace) -> int:
    application = _application()
    try:
        report = application.store.prune(apply=args.apply)
    finally:
        application.close()
    print(json.dumps({"applied": args.apply, **report}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(args)
        if args.command == "mcp":
            return _mcp()
        if args.command == "prune":
            return _prune(args)
        if args.server:
            data = _remote(args)
        elif args.command in ("status", "download", "cancel"):
            raise ValueError(f"{args.command} needs --server or ACADEMIC_SOURCE_SERVER")
        else:
            data = _local(args)
    except _ServerExtraMissing:
        server_only = args.command in ("serve", "mcp", "prune")
        print(_SERVER_EXTRA if server_only else _NO_SERVER, file=sys.stderr)
        return 1
    except (
        OSError,
        httpx.HTTPError,
        httpx.InvalidURL,
        ValueError,
        RuntimeError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(data, ensure_ascii=True, indent=2))
    if args.command in ("get", "batch"):
        return 0 if data["status"] == "succeeded" else 1
    return 0
