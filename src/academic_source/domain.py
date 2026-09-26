"""Public contracts shared by the application and its transports."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

Policy = Literal[
    "fastest", "oa_first", "legal_only", "scihub_first", "grey_only", "scihub_only"
]
JobStatus = Literal[
    "queued", "running", "succeeded", "partial", "failed", "interrupted"
]


class Provenance(BaseModel):
    source: str
    url: str | None = None
    acquired_at: str
    derived_from: str | None = None


class Artifact(BaseModel):
    id: str
    kind: str
    media_type: str
    filename: str
    size: int
    identifier: str | None = None
    provenance: Provenance


class Upload(BaseModel):
    id: str
    filename: str
    size: int


class Attempt(BaseModel):
    source: str
    status: str
    reason: str = ""
    message: str = ""
    action: str = ""


class AcquisitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifiers: list[str] = Field(default_factory=list)
    upload_id: str | None = None
    text: str | None = None
    policy: Policy = "fastest"
    markdown: bool = False
    supplementary: bool = False
    bibtex: bool = False
    resolve_titles: bool = True

    @model_validator(mode="after")
    def check_input(self) -> "AcquisitionRequest":
        self.identifiers = [
            value.strip() for value in self.identifiers if value.strip()
        ]
        if (
            sum(
                (
                    bool(self.identifiers),
                    bool(self.upload_id),
                    bool(self.text and self.text.strip()),
                )
            )
            != 1
        ):
            raise ValueError("Provide exactly one of identifiers, upload_id, or text")
        return self


class AcquisitionResult(BaseModel):
    identifier: str
    status: Literal["succeeded", "failed"]
    artifacts: list[Artifact] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    reason: str = ""
    message: str = ""
    action: str = ""
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    cached: bool = False


class Job(BaseModel):
    id: str
    status: JobStatus = "queued"
    request: AcquisitionRequest
    total: int = 0
    completed: int = 0
    results: list[AcquisitionResult] = Field(default_factory=list)
    error: str = ""
    created_at: str
    updated_at: str

    @computed_field
    @property
    def artifacts(self) -> list[Artifact]:
        return [artifact for result in self.results for artifact in result.artifacts]
