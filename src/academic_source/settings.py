"""Runtime settings, separate from request policy and legacy source settings."""

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


def _api_keys(value: Any) -> list[SecretStr]:
    if not isinstance(value, list):
        # Pydantic and the CLI handle invalid configuration as ValueError.
        raise ValueError(  # noqa: TRY004
            "API keys must be a JSON array of non-empty ASCII strings"
        )
    keys = []
    for item in value:
        key = item.get_secret_value() if isinstance(item, SecretStr) else item
        if (
            not isinstance(key, str)
            or not key
            or any(not 33 <= ord(character) <= 126 for character in key)
        ):
            raise ValueError("API keys must be a JSON array of non-empty ASCII strings")
        keys.append(SecretStr(key))
    return keys


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    auth_mode: Literal["required", "off"] = "required"
    api_keys: list[SecretStr] = Field(default_factory=list, exclude=True, repr=False)
    public_base_url: str = ""

    data_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("ACADEMIC_SOURCE_DATA_DIR", "~/.academic-source")
        ).expanduser()
    )
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    interactive: bool = False
    source_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("api_keys", mode="before")
    @classmethod
    def validate_api_keys(cls, value: Any) -> list[SecretStr]:
        return _api_keys(value)

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
        root = cls(**({"data_dir": data_dir} if data_dir is not None else {})).data_dir
        path = root / "settings.json"
        values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        values["data_dir"] = root
        for field, variable in (
            ("auth_mode", "ACADEMIC_SOURCE_AUTH_MODE"),
            ("public_base_url", "ACADEMIC_SOURCE_PUBLIC_BASE_URL"),
        ):
            if variable in os.environ:
                values[field] = os.environ[variable]
        if "ACADEMIC_SOURCE_API_KEYS" in os.environ:
            try:
                keys = json.loads(os.environ["ACADEMIC_SOURCE_API_KEYS"])
            except (ValueError, TypeError):
                raise ValueError(
                    "ACADEMIC_SOURCE_API_KEYS must be a JSON array of non-empty ASCII strings"
                ) from None
            values["api_keys"] = _api_keys(keys)
        return cls.model_validate(values)
