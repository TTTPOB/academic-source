"""Safe, structured failures from upstream discovery services."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

RateLimitKind = Literal["daily_budget", "request_rate", "unknown"]


def source_label(source: str) -> str:
    return {"openalex": "OpenAlex", "crossref": "Crossref", "arxiv": "arXiv"}.get(
        source, source
    )


class DiscoveryError(RuntimeError):
    """Discovery failed rather than returning an empty set of matches."""


class UpstreamError(DiscoveryError):
    """An expected upstream failure without URLs, credentials, or raw bodies."""

    def __init__(self, source: str, message: str, *, status_code: int | None = None):
        self.source = source
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class RateLimited(UpstreamError):
    """A rejected operation and the upstream evidence about recovery."""

    def __init__(
        self,
        source: str,
        *,
        kind: RateLimitKind = "unknown",
        retry_at: float | None = None,
        remaining: int | None = None,
        required: int | None = None,
    ):
        self.kind = kind
        self.retry_at = retry_at
        self.remaining = remaining
        self.required = required
        description = {
            "daily_budget": "daily budget is insufficient for this operation",
            "request_rate": "request rate limit exceeded",
            "unknown": "upstream limit reached",
        }[kind]
        message = f"{source_label(source)}: {description} (HTTP 429)"
        if retry_at is not None:
            message += f"; retry at {_utc_time(retry_at)}"
        if remaining is not None and required is not None:
            message += f"; remaining={remaining}, required={required}"
        super().__init__(source, message, status_code=429)

    def to_dict(self) -> dict[str, str | int]:
        """Return only safe, JSON-compatible recovery information."""
        result: dict[str, str | int] = {"source": self.source, "kind": self.kind}
        if self.retry_at is not None:
            result["retry_at"] = _utc_time(self.retry_at)
        if self.remaining is not None:
            result["remaining"] = self.remaining
        if self.required is not None:
            result["required"] = self.required
        return result


def _utc_time(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
