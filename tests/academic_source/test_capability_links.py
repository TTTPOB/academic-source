"""Download capability behavior across real Application, Store, MCP, and HTTP."""

import asyncio
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from academic_source.app import create_app
from academic_source.interfaces.mcp import create_mcp
from academic_source.services.application import Application
from academic_source.services.downloads import DownloadCapabilities
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource


def mcp_call(client, name, arguments):
    response = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


@pytest.fixture
def service(tmp_path):
    application = Application(
        Settings(data_dir=tmp_path / "data", auth_mode="off"),
        source=RecordingSource(),
    )
    now = [1_800_000_000.0]
    application.downloads = DownloadCapabilities(
        application.store, clock=lambda: now[0]
    )
    try:
        yield application, now
    finally:
        application.close()


def acquire(client, identifiers=None):
    return mcp_call(
        client,
        "acquire",
        {
            "request": {"identifiers": identifiers or ["10.1234/example"]},
            "wait_seconds": 5,
        },
    )


def poll(client, job):
    return mcp_call(client, "job_status", {"job_id": job["id"]})


def test_mcp_reuses_one_ticket_per_artifact_and_renews_after_consumption(service):
    application, now = service
    with TestClient(create_app(application=application)) as client:
        job = acquire(client, ["10.1234/example", "10.1234/example"])
        artifact = job["artifacts"][0]
        url = artifact["download_url"]
        assert url.startswith("http://testserver/downloads/")
        assert urlsplit(url).path.rsplit("/", 1)[1] != artifact["id"]
        assert artifact["download_single_use"] is True
        assert (
            datetime.fromisoformat(artifact["download_expires_at"]).timestamp()
            == now[0] + 21600
        )
        assert all(item == artifact for item in job["artifacts"])
        assert all(result["artifacts"] == [artifact] for result in job["results"])
        now[0] += 20
        assert poll(client, job)["artifacts"][0] == artifact
        download = client.get(url)
        assert download.status_code == 200
        assert download.content == application.source.content
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["cache-control"] == "no-store"
        assert download.headers["referrer-policy"] == "no-referrer"
        assert download.headers["accept-ranges"] == "none"
        used = client.get(url)
        unknown = client.get("/downloads/not-a-ticket")
        assert used.status_code == unknown.status_code == 404
        assert used.json() == unknown.json() == {"detail": "Download not found"}
        assert (
            used.headers["cache-control"]
            == unknown.headers["cache-control"]
            == "no-store"
        )
        renewed = poll(client, job)["artifacts"][0]["download_url"]
        assert renewed != url
        assert client.get(renewed).content == application.source.content
        permanent = client.get(f"/api/v1/artifacts/{artifact['id']}/content")
        assert permanent.content == application.source.content
        assert application.store.artifact_path(artifact["id"]).is_file()


def test_head_post_and_range_do_not_consume_and_conditional_get_is_full(service):
    application, _ = service
    with TestClient(create_app(application=application)) as client:
        job = acquire(client)
        url = job["artifacts"][0]["download_url"]
        for method in ("HEAD", "POST"):
            rejected = client.request(method, url)
            assert rejected.status_code == 405
            assert rejected.headers["cache-control"] == "no-store"
        rejected = client.get(url, headers={"Range": "bytes=0-1"})
        assert rejected.status_code == 400
        assert rejected.headers["cache-control"] == "no-store"
        assert poll(client, job)["artifacts"][0]["download_url"] == url
        response = client.get(
            url,
            headers={
                "If-None-Match": "*",
                "If-Modified-Since": "Wed, 01 Jan 2099 00:00:00 GMT",
            },
        )
        assert response.status_code == 200
        assert response.content == application.source.content
        assert client.get(url).status_code == 404
        permanent = f"/api/v1/artifacts/{job['artifacts'][0]['id']}/content"
        response = client.get(permanent, headers={"Range": "bytes=0-1"})
        assert response.status_code == 206
        assert response.content == application.source.content[:2]


def test_expiry_renews_without_sleep_and_unregistered_or_missing_files_cannot_issue(
    service,
):
    application, now = service
    with TestClient(create_app(application=application)) as client:
        job = acquire(client)
        artifact = job["artifacts"][0]
        now[0] += 21600
        expired = client.get(artifact["download_url"])
        assert expired.status_code == 404
        renewed = poll(client, job)["artifacts"][0]["download_url"]
        assert renewed != artifact["download_url"]
        assert client.get(renewed).content == application.source.content
        with pytest.raises(KeyError):
            application.downloads.issue("not-registered", base_url="http://testserver")
        application.store.artifact_path(artifact["id"]).unlink()
        with pytest.raises(KeyError):
            application.downloads.issue(artifact["id"], base_url="http://testserver")


def test_restart_invalidates_ticket_but_preserves_job_artifact_and_cache(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", auth_mode="off")
    first = Application(settings, source=RecordingSource())
    with TestClient(create_app(application=first)) as client:
        job = acquire(client)
        artifact = job["artifacts"][0]
        token = urlsplit(artifact["download_url"]).path.rsplit("/", 1)[1]
        original = first.source.content
        assert token not in first.job(job["id"]).model_dump_json()
        assert (
            "download_url"
            not in first.store.get_artifact(artifact["id"]).model_dump_json()
        )
        with sqlite3.connect(settings.data_dir / "catalog.sqlite") as db:
            persisted = "\n".join(db.iterdump())
        assert token not in persisted
        assert (
            "download_url" not in persisted and "download_expires_at" not in persisted
        )
    second = Application(settings, source=RecordingSource())
    with TestClient(create_app(application=second)) as client:
        assert client.get(artifact["download_url"]).status_code == 404
        restored = poll(client, job)
        assert restored["artifacts"][0]["download_url"] != artifact["download_url"]
        assert client.get(restored["artifacts"][0]["download_url"]).content == original
        cached = acquire(client)
        assert cached["results"][0]["cached"] is True
        assert second.source.calls == []


def test_request_base_includes_root_path_and_public_base_overrides_it(service):
    application, _ = service
    with TestClient(
        create_app(application=application),
        base_url="https://localhost:8443",
        root_path="/papers",
    ) as client:
        job = acquire(client)
        url = job["artifacts"][0]["download_url"]
        assert url.startswith("https://localhost:8443/papers/downloads/")
        response = client.get(url)
        assert response.content == application.source.content
        assert response.headers["cache-control"] == "no-store"
        application.settings.public_base_url = "https://public.example/service/"
        url = poll(client, job)["artifacts"][0]["download_url"]
        assert url.startswith("https://public.example/service/downloads/")


def test_partial_stream_consumes_ticket_without_removing_artifact(service, tmp_path):
    application, _ = service
    original = b"download-content\n" * 20000
    source = tmp_path / "large.bin"
    source.write_bytes(original)
    artifact = application.store.import_artifact(
        source, kind="supplementary", identifier=None, source="offline-fixture"
    )
    url = application.downloads.issue(artifact.id, base_url="http://testserver")[
        "download_url"
    ]
    app = create_app(application=application)
    chunks = []

    async def interrupted_request():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                chunks.append(message["body"])
                raise OSError("client disconnected")

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": urlsplit(url).path,
                "raw_path": urlsplit(url).path.encode(),
                "root_path": "",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1234),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )

    with pytest.raises(OSError, match="client disconnected"):
        asyncio.run(interrupted_request())
    assert 0 < len(chunks[0]) < len(original)
    with TestClient(app) as client:
        assert client.get(url).status_code == 404
        permanent = f"/api/v1/artifacts/{artifact.id}/content"
        assert client.get(permanent).content == original
        renewed = application.downloads.issue(
            artifact.id, base_url="http://testserver"
        )["download_url"]
        assert client.get(renewed).content == original


def test_concurrent_gets_claim_once(service):
    application, _ = service
    with TestClient(create_app(application=application)) as client:
        url = acquire(client)["artifacts"][0]["download_url"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: client.get(url), range(2)))
        assert sorted(response.status_code for response in responses) == [200, 404]
        assert (
            next(
                response.content
                for response in responses
                if response.status_code == 200
            )
            == application.source.content
        )


def test_stdio_keeps_export_and_never_issues_http_capability(
    service, tmp_path, monkeypatch
):
    application, _ = service

    def forbidden(*args, **kwargs):
        pytest.fail("stdio must not issue HTTP capabilities")

    monkeypatch.setattr(application.downloads, "issue", forbidden)
    mcp = create_mcp(application, stdio=True)

    async def exchange():
        _, job = await mcp.call_tool(
            "acquire",
            {"request": {"identifiers": ["10.1234/example"]}, "wait_seconds": 5},
        )
        assert "download_url" not in str(job)
        artifact = job["artifacts"][0]
        _, status = await mcp.call_tool("job_status", {"job_id": job["id"]})
        assert "download_url" not in str(status)
        _, exported = await mcp.call_tool(
            "export_artifacts",
            {"artifact_ids": [artifact["id"]], "output_dir": str(tmp_path / "export")},
        )
        return exported["result"][0]

    exported = asyncio.run(exchange())
    from pathlib import Path

    assert Path(exported["path"]).read_bytes() == application.source.content


def test_capability_is_anonymous_but_permanent_artifact_remains_protected(tmp_path):
    application = Application(
        Settings(
            data_dir=tmp_path, auth_mode="required", api_keys=["test-service-key"]
        ),
        source=RecordingSource(),
    )
    with TestClient(create_app(application=application)) as client:
        unauthorized = client.post("/mcp")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["cache-control"] == "no-store"
        client.headers["Authorization"] = "Bearer test-service-key"
        job = acquire(client)
        artifact = job["artifacts"][0]
        permanent = f"/api/v1/artifacts/{artifact['id']}/content"
        assert client.get(permanent).content == application.source.content
        del client.headers["Authorization"]
        assert client.get(permanent).status_code == 401
        assert client.get(f"/downloads/{artifact['id']}").status_code == 404
        response = client.get(artifact["download_url"])
        assert response.status_code == 200
        assert response.content == application.source.content
        assert client.get(artifact["download_url"]).status_code == 404
        assert client.get("/downloads/not-a-ticket").status_code == 404
        assert client.get(f"/api/v1/jobs/{job['id']}").status_code == 401


def test_uvicorn_access_log_redacts_downloads_including_root_path(service, caplog):
    application, _ = service
    create_app(application=application)
    logger = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        for target in (
            "/downloads/secret-ticket",
            "/papers/downloads/secret-ticket?another=secret",
        ):
            logger.info('%s - "%s %s HTTP/%s" %d', "client", "GET", target, "1.1", 200)
        logger.info(
            '%s - "%s %s HTTP/%s" %d', "client", "GET", "/api/v1/status", "1.1", 200
        )
    assert "secret-ticket" not in caplog.text and "another=secret" not in caplog.text
    assert "/papers/downloads/[redacted]" in caplog.text
    assert "/api/v1/status" in caplog.text
