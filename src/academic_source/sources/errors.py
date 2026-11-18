"""Typed provider failures and their stable acquisition diagnostics."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, assert_never

from scansci_pdf.upstream_errors import RateLimited as UpstreamRateLimited
from scansci_pdf.upstream_errors import RateLimitKind


@dataclass(frozen=True, kw_only=True)
class _Diagnostic:
    message: str = ""
    action: str = ""


@dataclass(frozen=True)
class Paywall(_Diagnostic):
    pass


@dataclass(frozen=True)
class Blocked(_Diagnostic):
    kind: Literal["unspecified", "cloudflare", "challenge", "http"] = "unspecified"
    status_code: int | None = None


@dataclass(frozen=True)
class AuthRequired(_Diagnostic):
    kind: Literal["auth", "login"] = "auth"


@dataclass(frozen=True)
class RateLimited(_Diagnostic):
    source: str | None = None
    kind: RateLimitKind = "unknown"
    retry_at: float | None = None
    remaining: int | None = None
    required: int | None = None

    @classmethod
    def from_upstream(cls, error: UpstreamRateLimited) -> "RateLimited":
        return cls(
            source=error.source,
            kind=error.kind,
            retry_at=error.retry_at,
            remaining=error.remaining,
            required=error.required,
            message=str(error),
            action="retry_later",
        )

    def details(self) -> dict[str, str | int]:
        if self.source is None:
            return {}
        result: dict[str, str | int] = {"source": self.source, "kind": self.kind}
        if self.retry_at is not None:
            result["retry_at"] = (
                datetime.fromtimestamp(self.retry_at, UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        if self.remaining is not None:
            result["remaining"] = self.remaining
        if self.required is not None:
            result["required"] = self.required
        return result


@dataclass(frozen=True)
class ConfigNeeded(_Diagnostic):
    pass


@dataclass(frozen=True)
class NetworkError(_Diagnostic):
    status_code: int | None = None


@dataclass(frozen=True)
class InvalidPDF(_Diagnostic):
    kind: Literal["unreadable", "preview"] = "unreadable"


@dataclass(frozen=True)
class NotFound(_Diagnostic):
    kind: Literal["unspecified", "pdf"] = "unspecified"


@dataclass(frozen=True)
class BrowserError(_Diagnostic):
    kind: Literal["unavailable", "navigate"]


@dataclass(frozen=True)
class InternalError(_Diagnostic):
    pass


@dataclass(frozen=True)
class UnknownLegacyError(_Diagnostic):
    """Retain a borrowed provider's unrecognized reason without inventing a new one."""

    code: str


SourceError = (
    Paywall
    | Blocked
    | AuthRequired
    | RateLimited
    | ConfigNeeded
    | NetworkError
    | InvalidPDF
    | NotFound
    | BrowserError
    | InternalError
    | UnknownLegacyError
)


@dataclass(frozen=True)
class FailurePresentation:
    attempt_reason: str
    # Lower values retain precedence; unranked failures use the last informative attempt.
    priority: int | None = None
    result_reason: str | None = None
    result_message: str | None = None


def present_error(error: SourceError) -> FailurePresentation:
    """Preserve existing JSON codes, ranking, and result-only message overrides."""
    match error:
        case Blocked(kind="cloudflare"):
            return FailurePresentation("cloudflare_blocked", 0, "network_error")
        case Blocked():
            return FailurePresentation(
                "blocked",
                1,
                "network_error",
                "Public PDF requests were blocked; check the browser session or retry later.",
            )
        case AuthRequired(kind="login"):
            return FailurePresentation("login_required", 3, "auth_required")
        case AuthRequired():
            return FailurePresentation("auth_required", 2)
        case Paywall():
            return FailurePresentation("paywall", 4, "auth_required")
        case RateLimited():
            return FailurePresentation("rate_limited", 5)
        case ConfigNeeded():
            return FailurePresentation("config_needed", 6)
        case NetworkError():
            return FailurePresentation(
                "network_error",
                7,
                result_message="Public PDF requests failed; check the network or retry later.",
            )
        case InvalidPDF(kind="preview"):
            return FailurePresentation("suspicious_pdf")
        case InvalidPDF():
            return FailurePresentation(
                "invalid_pdf",
                8,
                result_message="The available response was not a readable PDF; try another source.",
            )
        case NotFound(kind="pdf"):
            return FailurePresentation(
                "no_pdf_found",
                result_message="Public locations did not return a PDF; try another access channel.",
            )
        case NotFound():
            return FailurePresentation("not_found")
        case BrowserError(kind="unavailable"):
            return FailurePresentation(
                "browser_unavailable", result_reason="unsupported"
            )
        case BrowserError():
            return FailurePresentation("navigate_failed")
        case InternalError():
            return FailurePresentation("internal_error")
        case UnknownLegacyError(code=code):
            return FailurePresentation(code)
    assert_never(error)
