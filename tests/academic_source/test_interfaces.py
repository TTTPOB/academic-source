"""Exercise real services and storage through HTTP, MCP, and the remote CLI."""

import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from fastapi.testclient import TestClient

from academic_source.app import create_app
from academic_source.interfaces import cli
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource


def service_at(path):
    source = RecordingSource()
    return Application(Settings(data_dir=path), source=source), source


def test_http_upload_parse_acquire_and_retrieve_without_shared_paths(
    tmp_path, monkeypatch
):
    from scansci_pdf import md_export

    monkeypatch.setattr(
        md_export,
        "pdf_to_markdown_detailed",
        lambda *args, **kwargs: ("# Structured text\n", []),
    )
    service, source = service_at(tmp_path / "server")
    with TestClient(create_app(application=service)) as client:
        uploaded = client.post(
            "/api/v1/uploads",
            files={
                "file": (
                    "papers.bib",
                    b"@article{x, doi={10.1234/example}, title={A paper}}",
                )
            },
        )
        assert uploaded.status_code == 201
        upload_id = uploaded.json()["id"]
        parsed = client.post("/api/v1/lists/parse", json={"upload_id": upload_id})
        assert parsed.json()[0]["identifier"] == "10.1234/example"
        submitted = client.post(
            "/api/v1/acquisitions", json={"upload_id": upload_id, "markdown": True}
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]
        service.wait(job_id, 5)
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        assert job["status"] == "succeeded"
        item = job["results"][0]["artifacts"][0]
        assert item["id"] == job["artifacts"][0]["id"]
        assert str(tmp_path) not in str(job)
        fetched = client.get(item["download_url"])
        assert fetched.content == source.content
        assert fetched.headers["content-type"] == "application/pdf"
        markdown = job["artifacts"][1]
        assert markdown["kind"] == "markdown"
        assert markdown["provenance"]["derived_from"] == item["id"]
        assert client.get(markdown["download_url"]).text == "# Structured text\n"
        broken = client.post(
            "/api/v1/uploads", files={"file": ("broken.xlsx", b"not an XLSX")}
        ).json()
        assert (
            client.post(
                "/api/v1/lists/parse", json={"upload_id": broken["id"]}
            ).status_code
            == 400
        )
        assert client.get("/api/v1/artifacts/unknown/content").status_code == 404
        service.store.artifact_path(item["id"]).unlink()
        assert client.get(item["download_url"]).status_code == 404
        assert (
            client.post("/api/v1/lists/parse", json={"text": "   "}).status_code == 400
        )
        assert (
            client.post(
                "/api/v1/acquisitions",
                json={"identifiers": ["10.1234/example"], "output_dir": "/client"},
            ).status_code
            == 422
        )


def test_mcp_and_http_share_one_real_job_and_artifact_store(tmp_path):
    service, source = service_at(tmp_path)
    with TestClient(create_app(application=service)) as client:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200, initialized.text
        # Stateless transport: no session is issued, so a client keeps working
        # after a server restart rather than re-initializing.
        assert "mcp-session-id" not in initialized.headers
        headers["mcp-session-id"] = "session-issued-before-a-restart"
        client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        tools = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert {tool["name"] for tool in tools.json()["result"]["tools"]} == {
            "search",
            "resolve",
            "parse_list",
            "acquire",
            "job_status",
        }
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "acquire",
                    "arguments": {
                        "request": {"identifiers": ["10.1234/example"]},
                        "wait_seconds": 0,
                    },
                },
            },
        )
        assert response.status_code == 200, response.text
        job = response.json()["result"]["structuredContent"]
        service.wait(job["id"], 5)
        assert (
            "download_url"
            in client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "job_status",
                        "arguments": {"job_id": job["id"]},
                    },
                },
            ).json()["result"]["structuredContent"]["artifacts"][0]
        )
        current = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert current["status"] == "succeeded"
        assert (
            client.get(current["artifacts"][0]["download_url"]).content
            == source.content
        )
        assert source.calls == ["10.1234/example"]
        assert (
            client.post(
                "/mcp/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
            ).status_code
            == 404
        )


@contextmanager
def running_server(application):
    # Bind once and give Uvicorn the socket; no fixed port or port-allocation race.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(create_app(application=application), log_level="error")
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while (
                not server.started and thread.is_alive() and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert server.started, "test HTTP server failed to start"
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "test HTTP server did not shut down"


def test_remote_cli_uploads_and_exports_over_real_http(tmp_path, monkeypatch):
    # This loopback test must not depend on the developer's proxy bypass syntax.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    service, source = service_at(tmp_path / "server")
    client_dir = tmp_path / "client"
    client_dir.mkdir()
    listing = client_dir / "papers.csv"
    listing.write_text("doi,title\n10.1234/example,Offline paper\n", encoding="utf-8")
    output = client_dir / "export"
    with running_server(service) as server:
        assert (
            cli.main(
                ["batch", str(listing), "--server", server, "--output", str(output)]
            )
            == 0
        )
    assert source.calls == ["10.1234/example"]
    files = list(output.glob("*.pdf"))
    assert len(files) == 1
    assert files[0].read_bytes() == source.content
    assert listing.read_text(encoding="utf-8").startswith("doi,title")


def test_mcp_transport_is_stateless_across_restarts(tmp_path):
    """A stale session id from a previous process must not lock a client out."""
    service, source = service_at(tmp_path)
    with TestClient(create_app(application=service)) as client:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "mcp-session-id": "session-issued-before-the-restart",
        }
        response = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200, response.text
        assert "mcp-session-id" not in response.headers
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
            "search",
            "resolve",
            "parse_list",
            "acquire",
            "job_status",
        }
        assert source.calls == []
