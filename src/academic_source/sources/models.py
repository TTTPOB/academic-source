"""Normalized acquisition outcomes at the source boundary."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain import Attempt

log = logging.getLogger("academic_source.sources")


def observe(identifier: str, attempt: Attempt, started: float) -> Attempt:
    """Stamp one attempt with its duration and write its log line.

    The message is left out on purpose: it may quote a location URL.
    """
    attempt.seconds = round(time.monotonic() - started, 3)
    log.info(
        "attempt identifier=%s source=%s status=%s reason=%s candidates=%s seconds=%.3f",
        identifier,
        attempt.source,
        attempt.status,
        attempt.reason or "-",
        "-" if attempt.candidates is None else attempt.candidates,
        attempt.seconds,
    )
    return attempt


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
    rate_limit: dict[str, Any] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)
    # Unpaywall and OpenAlex both say "not open access" and nothing was located.
    closed_access: bool = False
