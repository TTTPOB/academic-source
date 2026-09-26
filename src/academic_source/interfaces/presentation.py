"""Shared transport representation; internal storage paths never enter responses."""

from typing import Any

from academic_source.domain import Artifact, Job


def artifact_data(artifact: Artifact, *, downloadable: bool = True) -> dict[str, Any]:
    data = artifact.model_dump(mode="json")
    if downloadable:
        data["download_url"] = f"/api/v1/artifacts/{artifact.id}/content"
    return data


def job_data(job: Job, *, downloadable: bool = True) -> dict[str, Any]:
    data = job.model_dump(mode="json")
    data["artifacts"] = [
        artifact_data(artifact, downloadable=downloadable) for artifact in job.artifacts
    ]
    for result, original in zip(data["results"], job.results, strict=True):
        result["artifacts"] = [
            artifact_data(artifact, downloadable=downloadable)
            for artifact in original.artifacts
        ]
    return data
