"""Shared transport representation; internal storage paths never enter responses."""

from typing import Any

from academic_source.domain import Artifact, Job


def artifact_data(artifact: Artifact) -> dict[str, Any]:
    return {
        **artifact.model_dump(mode="json"),
        "download_url": f"/api/v1/artifacts/{artifact.id}/content",
    }


def job_data(job: Job) -> dict[str, Any]:
    data = job.model_dump(mode="json")
    data["artifacts"] = [artifact_data(artifact) for artifact in job.artifacts]
    for result, original in zip(data["results"], job.results, strict=True):
        result["artifacts"] = [
            artifact_data(artifact) for artifact in original.artifacts
        ]
    return data
