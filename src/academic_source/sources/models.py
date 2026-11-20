"""Normalized acquisition outcomes at the source boundary."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain import Attempt
from .errors import (
    NotFound,
    RateLimited,
    SourceError,
    UnknownLegacyError,
    present_error,
)

if TYPE_CHECKING:
    from .oa_candidates import Candidate

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
    warnings: list[str] = field(default_factory=list)


ProviderOutcome = SourceSuccess | SourceError


def failed_attempt(source: str, error: SourceError) -> Attempt:
    return Attempt(
        source=source,
        status="failed",
        reason=present_error(error).attempt_reason,
        message=error.message,
        action=error.action,
        rate_limit=error.details() if isinstance(error, RateLimited) else {},
    )


@dataclass
class SourceFailure:
    reason: str
    message: str = ""
    action: str = ""
    rate_limit: dict[str, Any] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)
    # Unpaywall and OpenAlex both say "not open access" and nothing was located.
    closed_access: bool = False
    errors: list[SourceError] = field(default_factory=list, repr=False)
    # Preprint locations held back until every other source has failed.
    preprints: list[Candidate] = field(default_factory=list, repr=False)

    @classmethod
    def summarize(
        cls, errors: list[SourceError], attempts: list[Attempt]
    ) -> SourceFailure:
        # The first error of the highest priority wins. In the absence of a
        # ranked error, retain the most recent error that was more than a miss.
        selected: SourceError = NotFound()
        priority: int | None = None
        for error in errors:
            match error:
                case NotFound(kind="unspecified") | UnknownLegacyError(code=""):
                    continue
            current = present_error(error).priority
            if priority is None or (current is not None and current < priority):
                selected, priority = error, current
        presentation = present_error(selected)
        return cls(
            reason=presentation.result_reason or presentation.attempt_reason,
            message=presentation.result_message or selected.message,
            action=selected.action,
            rate_limit=selected.details() if isinstance(selected, RateLimited) else {},
            attempts=attempts,
        )
