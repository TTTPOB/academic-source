"""The typed errors preserve the existing diagnostic codes at both levels."""

import pytest

from academic_source.sources.errors import (
    AuthRequired,
    Blocked,
    BrowserError,
    ConfigNeeded,
    InternalError,
    InvalidPDF,
    NetworkError,
    NotFound,
    Paywall,
    RateLimited,
    UnknownLegacyError,
    present_error,
)


@pytest.mark.parametrize(
    "error,attempt_reason,result_reason,priority",
    [
        (Blocked("cloudflare"), "cloudflare_blocked", "network_error", 0),
        (Blocked("http", status_code=403), "blocked", "network_error", 1),
        (AuthRequired(), "auth_required", "auth_required", 2),
        (AuthRequired("login"), "login_required", "auth_required", 3),
        (Paywall(), "paywall", "auth_required", 4),
        (RateLimited(), "rate_limited", "rate_limited", 5),
        (ConfigNeeded(), "config_needed", "config_needed", 6),
        (NetworkError(), "network_error", "network_error", 7),
        (InvalidPDF(), "invalid_pdf", "invalid_pdf", 8),
        (InvalidPDF("preview"), "suspicious_pdf", "suspicious_pdf", None),
        (BrowserError("unavailable"), "browser_unavailable", "unsupported", None),
        (BrowserError("navigate"), "navigate_failed", "navigate_failed", None),
        (InternalError(), "internal_error", "internal_error", None),
        (NotFound("pdf"), "no_pdf_found", "no_pdf_found", None),
        (NotFound(), "not_found", "not_found", None),
        (UnknownLegacyError("example"), "example", "example", None),
    ],
)
def test_error_projection(error, attempt_reason, result_reason, priority):
    diagnostic = present_error(error)
    assert diagnostic.attempt_reason == attempt_reason
    assert (diagnostic.result_reason or diagnostic.attempt_reason) == result_reason
    assert diagnostic.priority == priority


def test_rate_limit_keeps_recovery_fields_when_available():
    error = RateLimited(
        source="openalex", kind="daily_budget", retry_at=0, remaining=0, required=42
    )
    assert error.details() == {
        "source": "openalex",
        "kind": "daily_budget",
        "retry_at": "1970-01-01T00:00:00Z",
        "remaining": 0,
        "required": 42,
    }
    assert RateLimited().details() == {}
