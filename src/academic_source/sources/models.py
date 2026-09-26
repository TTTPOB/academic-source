"""Normalized acquisition outcomes at the source boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain import Attempt


@dataclass
class SourceSuccess:
    path: Path
    source: str
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)


@dataclass
class SourceFailure:
    reason: str
    message: str = ""
    action: str = ""
    attempts: list[Attempt] = field(default_factory=list)
