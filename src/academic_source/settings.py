"""Runtime settings, separate from request policy and legacy source settings."""

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Settings(BaseModel):
    data_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("ACADEMIC_SOURCE_DATA_DIR", "~/.academic-source")
        ).expanduser()
    )
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    interactive: bool = False
    source_config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def legacy_worker_setting(cls, value: Any) -> Any:
        if isinstance(value, dict) and "job_workers" in value:
            if value["job_workers"] != 1:
                raise ValueError(
                    "job_workers is no longer configurable (only 1 supported)"
                )
            return {key: item for key, item in value.items() if key != "job_workers"}
        return value

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Settings":
        settings = cls(**({"data_dir": data_dir} if data_dir is not None else {}))
        path = settings.data_dir / "settings.json"
        if path.exists():
            settings = cls.model_validate(
                {
                    **json.loads(path.read_text(encoding="utf-8")),
                    "data_dir": settings.data_dir,
                }
            )
        return settings
