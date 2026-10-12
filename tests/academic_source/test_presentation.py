"""Agent-facing projections keep outcomes, not acquisition diagnostics."""

import copy

import pytest
from fastapi.testclient import TestClient

from academic_source.app import create_app
from academic_source.domain import (
    AcquisitionRequest,
    AcquisitionResult,
    Artifact,
    Attempt,
    Job,
    Provenance,
)
from academic_source.interfaces.presentation import job_data
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource


@pytest.fixture
def successful_result():
    return AcquisitionResult(
        identifier="10.1234/hit",
        status="succeeded",
        artifacts=[
            Artifact(
                id="paper-artifact",
                kind="pdf",
                media_type="application/pdf",
                filename="paper.pdf",
                size=100,
                identifier="10.1234/hit",
                provenance=Provenance(
                    source="repository",
                    url="https://example.org/paper.pdf",
                    acquired_at="2026-01-01T00:00:00Z",
                ),
            )
        ],
        attempts=[
            Attempt(source="first", status="failed", reason="not_found"),
            Attempt(source="second", status="failed", reason="network_error"),
            Attempt(source="first", status="failed", reason="not_found"),
            Attempt(source="repository", status="succeeded"),
        ],
        metadata={
            "title": "A real hit",
            "oa_locations": [
                {"url": "https://example.org/paper.pdf", "license": "cc-by"}
            ],
        },
        warnings=["Markdown unavailable; see server logs", "BibTeX unavailable"],
    )


def make_job(*results):
    successes = sum(result.status == "succeeded" for result in results)
    return Job(
        id="projection-job",
        request=AcquisitionRequest(
            identifiers=[result.identifier for result in results]
        ),
        status="succeeded"
        if successes == len(results)
        else "partial"
        if successes
        else "failed",
        total=len(results),
        completed=len(results),
        results=list(results),
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def test_success_keeps_hits_metadata_and_exports_without_attempts(successful_result):
    data = job_data(make_job(successful_result))
    result = data["results"][0]
    assert "attempts" not in result
    assert not {"reason", "message", "action"} & result.keys()
    assert result["metadata"] == successful_result.metadata
    assert result["warnings"] == successful_result.warnings
    assert result["cached"] is False
    artifact = result["artifacts"][0]
    assert artifact["provenance"]["source"] == "repository"
    assert artifact["provenance"]["url"] == "https://example.org/paper.pdf"
    assert artifact["download_url"] == "/api/v1/artifacts/paper-artifact/content"
    assert data["artifacts"] == result["artifacts"]
    assert "first" not in str(data) and "second" not in str(data)


def test_all_misses_keep_one_short_failure():
    result = AcquisitionResult(
        identifier="10.1234/miss",
        status="failed",
        reason="not_found",
        message="Every source returned an empty result",
        attempts=[
            Attempt(source=f"source-{i}", status="failed", reason="not_found")
            for i in range(20)
        ],
    )
    data = job_data(make_job(result))
    assert data["status"] == "failed"
    assert data["artifacts"] == []
    assert data["results"] == [
        {"identifier": "10.1234/miss", "status": "failed", "reason": "not_found"}
    ]


def test_partial_job_keeps_failed_paper_and_job_error(successful_result):
    job = make_job(
        successful_result,
        AcquisitionResult(
            identifier="10.1234/miss", status="failed", reason="invalid_document"
        ),
    )
    job.error = "Job execution failed; see server logs"
    data = job_data(job)
    assert data["status"] == "partial"
    assert data["completed"] == data["total"] == 2
    assert data["results"][1] == {
        "identifier": "10.1234/miss",
        "status": "failed",
        "reason": "invalid_document",
    }
    assert data["error"] == job.error


@pytest.mark.parametrize(
    "reason,action",
    [
        ("config_needed", ""),
        ("auth_required", ""),
        ("network_error", "check_proxy"),
        ("internal_error", ""),
    ],
)
def test_actionable_failures_keep_aggregate_instructions(reason, action):
    result = AcquisitionResult(
        identifier="10.1234/blocked",
        status="failed",
        reason=reason,
        message="Configure access or inspect server logs",
        action=action,
        attempts=[Attempt(source="not-useful", status="failed", reason="not_found")],
    )
    data = job_data(make_job(result))["results"][0]
    assert data["reason"] == reason
    assert data["message"] == result.message
    assert data.get("action", "") == action
    assert "attempts" not in data


def test_non_actionable_error_keeps_reason_and_warnings_without_failure_detail():
    result = AcquisitionResult(
        identifier="10.1234/error",
        status="failed",
        reason="network_error",
        message="A long source-specific failure transcript",
        warnings=["A requested export was not produced"],
    )
    assert job_data(make_job(result))["results"][0] == {
        "identifier": "10.1234/error",
        "status": "failed",
        "reason": "network_error",
        "warnings": ["A requested export was not produced"],
    }


def test_empty_reason_still_has_failure_summary():
    result = AcquisitionResult(identifier="10.1234/unknown", status="failed")
    assert job_data(make_job(result))["results"][0]["reason"] == "acquisition_failed"


def test_projection_does_not_mutate_internal_or_persisted_record(
    tmp_path, successful_result
):
    service = Application(Settings(data_dir=tmp_path), source=RecordingSource())
    try:
        job = make_job(successful_result)
        service.store.save_job(job)
        before = copy.deepcopy(job.model_dump(mode="json"))
        projected = job_data(job)
        projected["results"][0]["metadata"]["oa_locations"][0]["license"] = "changed"
        projected["results"][0]["warnings"].append("changed")
        projected["artifacts"][0]["provenance"]["source"] = "changed"
        assert job.model_dump(mode="json") == before
        assert service.job(job.id).model_dump(mode="json") == before
        assert len(service.job(job.id).results[0].attempts) == 4
    finally:
        service.close()


def test_http_and_mcp_status_share_projection(tmp_path, successful_result):
    source = RecordingSource()
    service = Application(Settings(data_dir=tmp_path, auth_mode="off"), source=source)
    original = tmp_path / "paper.pdf"
    original.write_bytes(source.content)
    successful_result.artifacts = [
        service.store.import_artifact(
            original,
            kind="pdf",
            identifier=successful_result.identifier,
            source="repository",
        )
    ]
    job = make_job(
        successful_result,
        AcquisitionResult(
            identifier="10.1234/miss", status="failed", reason="not_found"
        ),
    )
    service.store.save_job(job)
    with TestClient(create_app(application=service)) as client:
        http = client.get(f"/api/v1/jobs/{job.id}")
        assert http.status_code == 200
        mcp = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "job_status", "arguments": {"job_id": job.id}},
            },
        )
        assert mcp.status_code == 200, mcp.text
        projected = mcp.json()["result"]["structuredContent"]
        assert projected["artifacts"] == projected["results"][0]["artifacts"]
        for artifact in [
            *projected["artifacts"],
            *projected["results"][0]["artifacts"],
        ]:
            assert artifact.pop("download_url").startswith(
                "http://testserver/downloads/"
            )
            assert artifact.pop("download_single_use") is True
            assert artifact.pop("download_expires_at")
            artifact["download_url"] = f"/api/v1/artifacts/{artifact['id']}/content"
        assert projected == http.json() == job_data(job)
        assert "attempts" not in str(http.json())
        assert len(service.job(job.id).results[0].attempts) == 4


def test_download_factory_runs_once_for_repeated_artifact(successful_result):
    calls = []

    def issue(artifact_id):
        calls.append(artifact_id)
        return {"download_url": f"https://example.org/downloads/ticket-{len(calls)}"}

    job = make_job(successful_result, successful_result.model_copy(deep=True))
    data = job_data(job, download_factory=issue)
    assert calls == ["paper-artifact"]
    assert data["artifacts"][0] == data["artifacts"][1]
    assert all(
        result["artifacts"] == [data["artifacts"][0]] for result in data["results"]
    )
    assert "download_url" not in job.model_dump_json()


def test_stdio_projection_omits_download_urls_but_keeps_artifact_ids(successful_result):
    data = job_data(make_job(successful_result), downloadable=False)
    assert data["artifacts"][0]["id"] == "paper-artifact"
    assert "download_url" not in data["artifacts"][0]
    assert "download_url" not in data["results"][0]["artifacts"][0]
