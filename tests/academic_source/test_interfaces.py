"""Transport boundary tests without literature network traffic."""

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from academic_source.domain import (
    AcquisitionRequest,
    AcquisitionResult,
    Artifact,
    Job,
    Provenance,
    Upload,
)


class FakeStore:
    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, Path] = {}
        self.uploads: dict[str, bytes] = {}
        self.artifact = Artifact(
            id="artifact-1",
            kind="pdf",
            media_type="application/pdf",
            filename="example.pdf",
            size=4,
            provenance=Provenance(source="fixture", acquired_at="2026-01-01T00:00:00Z"),
        )
        self.files[self.artifact.id] = root / "secret" / "example.pdf"
        self.files[self.artifact.id].parent.mkdir(parents=True)
        self.files[self.artifact.id].write_bytes(b"%PDF")

    def put_upload(self, filename: str, stream: BytesIO) -> Upload:
        content = stream.read()
        self.uploads["upload-1"] = content
        return Upload(id="upload-1", filename=filename, size=len(content))

    def get_artifact(self, artifact_id: str) -> Artifact:
        if artifact_id != self.artifact.id:
            raise KeyError(artifact_id)
        return self.artifact

    def artifact_path(self, artifact_id: str) -> Path:
        return self.files[artifact_id]


class FakeApplication:
    def __init__(self, root: Path):
        self.store = FakeStore(root)
        self.submissions = 0
        self.closed = False
        self.request: AcquisitionRequest | None = None
        self.last_job: Job | None = None

    def search(self, query: str, limit: int = 10) -> list[dict]:
        return [{"title": query, "limit": limit}]

    def resolve(self, identifier: str) -> dict:
        return {"identifier": identifier}

    def parse_list(
        self, *, upload_id: str | None = None, text: str | None = None
    ) -> list[dict]:
        if upload_id is not None:
            if upload_id not in self.store.uploads:
                raise KeyError(upload_id)
            text = self.store.uploads[upload_id].decode()
        return [{"identifier": value} for value in (text or "").splitlines() if value]

    def submit(self, request: AcquisitionRequest) -> Job:
        self.submissions += 1
        self.request = request
        result = AcquisitionResult(
            identifier="10.1/example",
            status="succeeded",
            artifacts=[self.store.artifact],
        )
        now = datetime.now(UTC).isoformat()
        self.last_job = Job(
            id=str(uuid4()),
            status="succeeded",
            request=request,
            total=1,
            completed=1,
            results=[result],
            artifacts=[self.store.artifact],
            created_at=now,
            updated_at=now,
        )
        return self.last_job

    def job(self, job_id: str) -> Job:
        if self.last_job is None or job_id != self.last_job.id:
            raise KeyError(job_id)
        return self.last_job

    def wait(self, job_id: str, timeout: float = 0) -> Job:
        assert 0 <= timeout <= 10
        return self.job(job_id)

    def close(self) -> None:
        self.closed = True


def test_http_upload_job_artifact_roundtrip(tmp_path: Path) -> None:
    from academic_source.app import create_app

    service = FakeApplication(tmp_path)
    with TestClient(create_app(application=service)) as client:
        uploaded = client.post(
            "/api/v1/uploads", files={"file": ("papers.bib", b"10.1/example\n")}
        )
        assert uploaded.status_code == 201
        upload_id = uploaded.json()["id"]
        assert client.post(
            "/api/v1/lists/parse", json={"upload_id": upload_id}
        ).json() == [{"identifier": "10.1/example"}]
        response = client.post("/api/v1/acquisitions", json={"upload_id": upload_id})
        assert response.status_code == 202
        assert service.request is not None and service.request.upload_id == upload_id
        job = client.get(f"/api/v1/jobs/{response.json()['id']}").json()
        item = job["results"][0]["artifacts"][0]
        assert item["download_url"] == "/api/v1/artifacts/artifact-1/content"
        assert str(tmp_path) not in str(job)
        assert client.get(item["download_url"]).content == b"%PDF"
        assert client.get("/api/v1/artifacts/unknown/content").status_code == 404
    assert service.closed


def test_mcp_tools_use_same_service_at_exact_path(tmp_path: Path) -> None:
    from academic_source.app import create_app

    service = FakeApplication(tmp_path)
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
        session = initialized.headers.get("mcp-session-id")
        assert session
        headers["mcp-session-id"] = session
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
                    "arguments": {"request": {"identifiers": ["10.1/example"]}},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["structuredContent"]["artifacts"][0][
            "download_url"
        ].startswith("/api/v1/")
        assert service.submissions == 1
        assert (
            client.post(
                "/mcp/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
            ).status_code
            == 404
        )


def test_remote_cli_uploads_client_file_and_downloads_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    from academic_source.app import create_app
    from academic_source.interfaces import cli

    service = FakeApplication(tmp_path / "server")
    source = tmp_path / "client" / "papers.txt"
    source.parent.mkdir()
    source.write_text("10.1/example\n")
    output = tmp_path / "client" / "export"
    with TestClient(create_app(application=service)) as client:

        class RemoteClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return client

            def __exit__(self, *_args):
                return None

            def post(self, url, **kwargs):
                return client.post("/" + url.lstrip("/"), **kwargs)

            def get(self, url, **kwargs):
                return client.get("/" + url.lstrip("/"), **kwargs)

            def stream(self, method, url):
                return client.stream(method, "/" + url.lstrip("/"))

        monkeypatch.setattr(cli.httpx, "Client", RemoteClient)
        assert (
            cli.main(
                [
                    "batch",
                    str(source),
                    "--server",
                    "http://remote.invalid",
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
        assert service.request is not None and service.request.upload_id == "upload-1"
        assert service.store.uploads["upload-1"] == b"10.1/example\n"
        assert next(iter(output.glob("*.pdf"))).read_bytes() == b"%PDF"
