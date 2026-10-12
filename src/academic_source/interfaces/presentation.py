"""Shared transport representation; internal storage paths never enter responses."""

from typing import Any

from academic_source.domain import AcquisitionResult, Artifact, Job


def artifact_data(artifact: Artifact, *, downloadable: bool = True) -> dict[str, Any]:
    data = artifact.model_dump(mode="json")
    if downloadable:
        data["download_url"] = f"/api/v1/artifacts/{artifact.id}/content"
    return data


def _result_data(result: AcquisitionResult, *, downloadable: bool) -> dict[str, Any]:
    # Attempts are diagnostics, not agent results. Artifact provenance identifies hits.
    data = result.model_dump(mode="json", exclude={"attempts", "artifacts"})
    data["artifacts"] = [
        artifact_data(artifact, downloadable=downloadable)
        for artifact in result.artifacts
    ]
    if result.status == "succeeded":
        for key in ("reason", "message", "action"):
            data.pop(key)
    else:
        # Keep one aggregate outcome, not a source-by-source failure transcript.
        data.pop("cached")
        data["reason"] = result.reason or "acquisition_failed"
        # Internal errors carry the existing instruction to inspect server logs.
        actionable = bool(result.action) or result.reason in {
            "config_needed",
            "auth_required",
            "internal_error",
        }
        if not actionable:
            data.pop("message")
            data.pop("action")
    for key in ("artifacts", "metadata", "warnings", "message", "action"):
        if not data.get(key):
            data.pop(key, None)
    return data


def job_data(job: Job, *, downloadable: bool = True) -> dict[str, Any]:
    # Exclude diagnostics before serialization, including when polling large jobs.
    data = job.model_dump(mode="json", exclude={"results", "artifacts"})
    data["results"] = [
        _result_data(result, downloadable=downloadable) for result in job.results
    ]
    data["artifacts"] = [
        artifact_data(artifact, downloadable=downloadable) for artifact in job.artifacts
    ]
    return data
