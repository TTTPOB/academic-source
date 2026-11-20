"""Public contracts shared by the application and its transports."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

Policy = Literal[
    "fastest", "oa_first", "legal_only", "scihub_first", "grey_only", "scihub_only"
]
JobStatus = Literal[
    "queued", "running", "succeeded", "partial", "failed", "interrupted", "cancelled"
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
    note: str = ""


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
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    # Wall-clock duration of this attempt; absent on records written before it existed.
    seconds: float | None = None
    # status="preprint": the download worked but is a preprint, kept as a last resort.
    # How many candidate locations a metadata service returned: positive for
    # status="located", 0 for "no_open_copy". Older records say "discovered" for both.
    candidates: int | None = None
    # The service's own open-access verdict for the work, when it states one.
    open_access: bool | None = None


class AcquisitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifiers: list[str] = Field(default_factory=list)
    upload_id: str | None = None
    text: str | None = None
    policy: Policy = "fastest"
    markdown: bool = False
    supplementary: bool = False
    supplementary_only: bool = Field(
        default=False,
        description="Fetch only publisher supplementary files, without the main PDF or BibTeX.",
    )
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
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    cached: bool = False
    # Time spent on this paper once its turn came; queue time is not included.
    elapsed_seconds: float | None = None


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
    # Set when the worker picks the job up; created_at..started_at is queue time.
    started_at: str | None = None
    current_identifier: str = ""
    # Filled on read for queued jobs only; never persisted with a value.
    queue_ahead: int | None = None

    @computed_field
    @property
    def artifacts(self) -> list[Artifact]:
        return [artifact for result in self.results for artifact in result.artifacts]
