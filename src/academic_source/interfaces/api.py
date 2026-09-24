"""HTTP protocol translation; application services own the business logic."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from academic_source.domain import AcquisitionRequest, Artifact, Job

if TYPE_CHECKING:
    from academic_source.services.application import Application


class ResolveInput(BaseModel):
    identifier: str


class ParseInput(BaseModel):
    upload_id: str | None = None
    text: str | None = None


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


def create_router(application: Application) -> APIRouter:
    api = APIRouter(prefix="/api/v1")

    @api.get("/search")
    def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
        return application.search(query, limit)

    @api.post("/resolve")
    def resolve(payload: ResolveInput) -> dict[str, Any]:
        return application.resolve(payload.identifier)

    @api.post("/uploads", status_code=201)
    def upload(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        try:
            return application.store.put_upload(
                file.filename or "upload", file.file
            ).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            file.file.close()

    @api.post("/lists/parse")
    def parse_list(payload: ParseInput) -> list[dict[str, Any]]:
        try:
            return application.parse_list(
                upload_id=payload.upload_id, text=payload.text
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown upload") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/acquisitions", status_code=202)
    def acquire(payload: AcquisitionRequest) -> dict[str, Any]:
        try:
            return job_data(application.submit(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown upload") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        try:
            return job_data(application.job(job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown job") from exc

    @api.get("/artifacts/{artifact_id}/content")
    def artifact(artifact_id: str) -> FileResponse:
        try:
            item = application.store.get_artifact(artifact_id)
            path = application.store.artifact_path(artifact_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown artifact") from exc
        return FileResponse(path, media_type=item.media_type, filename=item.filename)

    return api
