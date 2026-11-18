"""Cancellation at job queue and paper boundaries, through both transports."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from academic_source.app import create_app
from academic_source.domain import AcquisitionRequest
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource


class BlockingSource(RecordingSource):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()

    def acquire(self, identifier, request, work_dir, config):
        if identifier == "10.1234/block":
            self.started.set()
            assert self.release.wait(10)
        return super().acquire(identifier, request, work_dir, config)


@pytest.fixture
def service(tmp_path):
    source = BlockingSource()
    application = Application(
        Settings(data_dir=tmp_path, auth_mode="off"), source=source
    )
    try:
        yield application, source
    finally:
        source.release.set()
        application.close()


def submit(application, *papers):
    return application.submit(
        AcquisitionRequest(identifiers=[f"10.1234/{paper}" for paper in papers])
    )


def test_cancel_queued_job_releases_waiter_and_updates_queue(service, monkeypatch):
    application, source = service
    submit(application, "block")
    assert source.started.wait(5)
    queued = submit(application, "skip")
    tail = submit(application, "tail")
    assert tail.queue_ahead == 2

    # Ensure wait() has retained the future before cancellation removes it.
    future = application._futures[queued.id]
    result = future.result
    waiting = Event()

    def notify_and_wait(timeout):
        waiting.set()
        return result(timeout=timeout)

    monkeypatch.setattr(future, "result", notify_and_wait)
    with ThreadPoolExecutor(max_workers=1) as executor:
        waiter = executor.submit(application.wait, queued.id, 5)
        assert waiting.wait(5)
        cancelled = application.cancel(queued.id)
        assert waiter.result(timeout=5) == cancelled

    assert future.cancelled()
    assert cancelled.status == "cancelled"
    assert cancelled.started_at is None
    assert cancelled.completed == 0
    assert cancelled.results == []
    assert application.store.get_job(queued.id) == cancelled
    assert application.job(tail.id).queue_ahead == 1
    assert application.cancel(queued.id) == cancelled

    source.release.set()
    completed = application.wait(tail.id, 5)
    assert completed.status == "succeeded"
    assert application.cancel(tail.id) == completed
    assert source.calls == ["10.1234/block", "10.1234/tail"]


def test_cancel_running_job_preserves_completed_papers_and_artifacts(service):
    application, source = service
    job = submit(application, "first", "block", "skip")
    assert source.started.wait(5)
    running = application.cancel(job.id)
    assert running.status == "running"
    assert running.current_identifier == "10.1234/block"
    assert running.completed == 1

    source.release.set()
    cancelled = application.wait(job.id, 5)
    assert cancelled.status == "cancelled"
    assert cancelled.current_identifier == ""
    assert cancelled.completed == 2
    assert cancelled.total == 3
    assert source.calls == ["10.1234/first", "10.1234/block"]
    assert [result.identifier for result in cancelled.results] == source.calls
    assert [artifact.identifier for artifact in cancelled.artifacts] == source.calls
    for artifact in cancelled.artifacts:
        assert (
            application.store.artifact_path(artifact.id).read_bytes() == source.content
        )

    application.close()
    restarted = Application(application.settings, source=RecordingSource())
    try:
        assert restarted.job(job.id) == cancelled
    finally:
        restarted.close()


def test_cancel_http_and_mcp_return_status_projection(service):
    application, source = service
    with TestClient(create_app(application=application)) as client:
        try:
            submit(application, "block")
            assert source.started.wait(5)

            with pytest.raises(KeyError, match="unknown"):
                application.cancel("unknown")
            assert client.post("/api/v1/jobs/unknown/cancel").status_code == 404

            http_job = submit(application, "skip-http")
            path = f"/api/v1/jobs/{http_job.id}"
            response = client.post(f"{path}/cancel")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "cancelled"
            assert "queue" not in data and "started_at" not in data
            assert data == client.get(path).json()

            mcp_job = submit(application, "skip-mcp")

            def mcp(tool):
                response = client.post(
                    "/mcp",
                    headers={"Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": {"job_id": mcp_job.id}},
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()["result"]["structuredContent"]

            data = mcp("cancel_job")
            assert data["status"] == "cancelled"
            assert data == mcp("job_status")
        finally:
            source.release.set()
