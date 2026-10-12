"""Verify the HTTP security boundary without changing local or stdio behavior."""

import asyncio
import json
import logging

import pytest
import requests
from fastapi.testclient import TestClient
from pydantic import SecretStr

from academic_source.app import create_app
from academic_source.interfaces.auth import BearerAuthMiddleware
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource

KEYS = ["offline-client-one", "offline-client-two"]


@pytest.fixture
def service(tmp_path):
    application = Application(
        Settings(data_dir=tmp_path, api_keys=KEYS), source=RecordingSource()
    )
    try:
        yield application
    finally:
        application.close()


def test_required_default_fails_before_creating_application_files(tmp_path):
    root = tmp_path / "not-created"
    settings = Settings(data_dir=root)
    assert settings.auth_mode == "required"
    assert settings.api_keys == []
    with pytest.raises(ValueError, match="requires at least one API key"):
        create_app(settings)
    assert not root.exists()


def test_explicit_off_allows_http_and_local_settings_need_no_keys(tmp_path):
    # Constructing and running the local application never requires HTTP keys.
    local = Application(Settings(data_dir=tmp_path / "local"), source=RecordingSource())
    local.close()
    service = Application(
        Settings(data_dir=tmp_path / "http", auth_mode="off"), source=RecordingSource()
    )
    with TestClient(create_app(application=service)) as client:
        assert client.get("/api/v1/status").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert client.post(
            "/api/v1/lists/parse", json={"text": "10.1234/example"}
        ).json()


def test_settings_load_environment_precedes_file_but_not_explicit_construction(
    tmp_path, monkeypatch
):
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "auth_mode": "off",
                "api_keys": ["offline-file-key"],
                "public_base_url": "https://file.example/prefix",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ACADEMIC_SOURCE_AUTH_MODE", "required")
    monkeypatch.setenv("ACADEMIC_SOURCE_API_KEYS", json.dumps(KEYS))
    monkeypatch.setenv("ACADEMIC_SOURCE_PUBLIC_BASE_URL", "https://env.example/prefix")
    loaded = Settings.load(tmp_path)
    assert loaded.auth_mode == "required"
    assert [key.get_secret_value() for key in loaded.api_keys] == KEYS
    assert loaded.public_base_url == "https://env.example/prefix"
    explicit = Settings(data_dir=tmp_path, auth_mode="off", api_keys=["explicit-key"])
    assert explicit.auth_mode == "off"
    assert explicit.api_keys[0].get_secret_value() == "explicit-key"
    assert explicit.public_base_url == ""

    monkeypatch.setenv("ACADEMIC_SOURCE_API_KEYS", "[]")
    with pytest.raises(ValueError, match="requires at least one API key"):
        create_app(Settings.load(tmp_path))
    monkeypatch.delenv("ACADEMIC_SOURCE_API_KEYS")
    assert Settings.load(tmp_path).api_keys[0].get_secret_value() == "offline-file-key"


@pytest.mark.parametrize(
    "value",
    [
        "private-invalid-json",
        '"private-not-a-list"',
        '["private-valid", ""]',
        '["private valid"]',
        '["private\\nvalue"]',
        '["private-非ASCII"]',
        '["private-valid", 123]',
        '{"private-key":"value"}',
    ],
)
def test_invalid_secret_environment_errors_do_not_echo_values(
    tmp_path, monkeypatch, value
):
    monkeypatch.setenv("ACADEMIC_SOURCE_API_KEYS", value)
    with pytest.raises(ValueError) as error:
        Settings.load(tmp_path)
    assert "private" not in str(error.value)
    assert "private" not in repr(error.value)


def test_programmatic_secret_errors_and_settings_output_are_redacted(tmp_path):
    with pytest.raises(ValueError) as error:
        Settings(api_keys=["private valid"])
    assert "private" not in str(error.value)
    assert "private" not in repr(error.value)
    settings = Settings(data_dir=tmp_path, api_keys=[SecretStr(KEYS[0]), KEYS[1]])
    for text in (
        str(settings),
        repr(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
    ):
        assert all(key not in text for key in KEYS)
    assert "api_keys" not in settings.model_dump()


def test_all_actual_http_surfaces_and_mcp_methods_require_bearer(service):
    protected = [
        ("GET", "/"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/openapi.json"),
        ("GET", "/api/v1/search?query=example"),
        ("POST", "/api/v1/metadata"),
        ("POST", "/api/v1/uploads"),
        ("POST", "/api/v1/lists/parse"),
        ("POST", "/api/v1/acquisitions"),
        ("GET", "/api/v1/jobs/unknown"),
        ("GET", "/api/v1/artifacts/unknown/content"),
        ("HEAD", "/api/v1/artifacts/unknown/content"),
        ("GET", "/api/v1/status/"),
        ("HEAD", "/api/v1/status"),
        ("POST", "/api/v1/status"),
        *((method, "/mcp") for method in ("GET", "POST", "DELETE", "HEAD", "OPTIONS")),
    ]
    with TestClient(create_app(application=service)) as client:
        for method, path in protected:
            for headers in ({}, {"Authorization": "Bearer incorrect-key"}):
                response = client.request(method, path, headers=headers)
                assert response.status_code == 401, (method, path, response.text)
                assert response.headers["www-authenticate"] == "Bearer"
                if method != "HEAD":
                    assert response.json() == {"detail": "Unauthorized"}
        assert client.get("/api/v1/status").json() == {
            "status": "ok",
            "application": "academic-source",
        }


def test_only_authorization_bearer_is_accepted(service):
    with TestClient(create_app(application=service)) as client:
        for headers in (
            {"X-API-Key": KEYS[0]},
            {"Authorization": f"Basic {KEYS[0]}"},
            {"Authorization": "Bearer"},
            {"Authorization": f"Bearer {KEYS[0]} extra"},
            {"CF-Access-Client-Id": "offline", "CF-Access-Client-Secret": "offline"},
            [
                ("Authorization", f"Bearer {KEYS[0]}"),
                ("Authorization", f"Bearer {KEYS[1]}"),
            ],
        ):
            assert client.get("/openapi.json", headers=headers).status_code == 401
        assert client.get(f"/openapi.json?api_key={KEYS[0]}").status_code == 401


def test_each_key_can_upload_initialize_mcp_and_list_tools(service, caplog):
    caplog.set_level(logging.INFO)
    with TestClient(create_app(application=service)) as client:
        for key in KEYS:
            headers = {"Authorization": f"Bearer {key}"}
            uploaded = client.post(
                "/api/v1/uploads",
                headers=headers,
                files={"file": ("references.txt", b"10.1234/example")},
            )
            assert uploaded.status_code == 201
            parsed = client.post(
                "/api/v1/lists/parse",
                headers=headers,
                json={"upload_id": uploaded.json()["id"]},
            )
            assert parsed.status_code == 200
            assert parsed.json()[0]["identifier"] == "10.1234/example"
            assert client.get("/openapi.json", headers=headers).status_code == 200
            headers["Accept"] = "application/json, text/event-stream"
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
                        "clientInfo": {"name": "auth-test", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200, initialized.text
            assert (
                initialized.json()["result"]["serverInfo"]["name"] == "academic-source"
            )
            tools = client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert tools.status_code == 200
            assert "acquire" in {
                tool["name"] for tool in tools.json()["result"]["tools"]
            }
    assert all(key not in caplog.text for key in KEYS)


def test_download_namespace_is_public_but_not_permanent_artifacts_under_root_path(
    service,
):
    with TestClient(create_app(application=service), root_path="/prefix") as client:
        assert client.get("/prefix/api/v1/status").status_code == 200
        assert client.get("/prefix/downloads/unknown").status_code == 404
        assert client.head("/prefix/downloads/unknown").status_code == 405
        assert client.post("/prefix/downloads/unknown").status_code == 405
        assert (
            client.get("/prefix/downloads/api/v1/artifacts/unknown/content").status_code
            == 404
        )
        assert client.get("/prefix/api/v1/artifacts/unknown/content").status_code == 401
        assert client.get("/prefix/downloads-other/unknown").status_code == 401
        assert client.get("/prefix/downloads").status_code == 401


def test_incoming_key_is_not_forwarded_to_academic_upstream(service, monkeypatch):
    sent = []

    def upstream(_session, request, **_kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"results": []}'
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr(requests.Session, "send", upstream)
    with TestClient(create_app(application=service)) as client:
        response = client.get(
            "/api/v1/search?query=example",
            headers={"Authorization": f"Bearer {KEYS[0]}"},
        )
        assert response.status_code == 200
    assert sent
    for request in sent:
        assert "Authorization" not in request.headers
        assert all(key not in request.url for key in KEYS)


def test_guard_passes_stream_chunks_before_completion_without_reading_body():
    async def exchange():
        first_chunk = asyncio.Event()
        received = []

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send(
                {"type": "http.response.body", "body": b"first", "more_body": True}
            )
            # Completion depends on the client already receiving the first chunk.
            await first_chunk.wait()
            await send(
                {"type": "http.response.body", "body": b"last", "more_body": False}
            )

        async def receive():
            pytest.fail("Authentication must not read or buffer the request body")

        async def send(message):
            received.append(message)
            if message.get("body") == b"first":
                first_chunk.set()

        guard = BearerAuthMiddleware(downstream, api_keys=[SecretStr(KEYS[0])])
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "root_path": "",
            "headers": [(b"authorization", f"Bearer {KEYS[0]}".encode())],
        }
        await asyncio.wait_for(guard(scope, receive, send), timeout=1)
        assert [message["body"] for message in received if "body" in message] == [
            b"first",
            b"last",
        ]
        received.clear()
        scope["headers"] = []
        await guard(scope, receive, send)
        assert received[0]["status"] == 401

    asyncio.run(exchange())
