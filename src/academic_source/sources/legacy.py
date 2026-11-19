"""Adapt borrowed source functions to the application provider contract."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import requests

from scansci_pdf.upstream_errors import RateLimited as UpstreamRateLimited

from ..infrastructure.documents import is_readable_pdf
from .errors import (
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
    SourceError,
    UnknownLegacyError,
)
from .models import ProviderOutcome, SourceSuccess

LegacyHandler = Callable[[str, Path, dict[str, Any]], dict[str, Any] | None]


def _legacy_error(code: str, *, message: str = "", action: str = "") -> SourceError:
    match code:
        case "cloudflare_blocked":
            return Blocked("cloudflare", message=message, action=action)
        case "blocked":
            return Blocked(message=message, action=action)
        case "auth_required":
            return AuthRequired(message=message, action=action)
        case "login_required":
            return AuthRequired("login", message=message, action=action)
        case "paywall":
            return Paywall(message=message, action=action)
        case "rate_limited":
            return RateLimited(message=message, action=action)
        case "config_needed":
            return ConfigNeeded(message=message, action=action)
        case "network_error":
            return NetworkError(message=message, action=action)
        case "invalid_pdf":
            return InvalidPDF(message=message, action=action)
        case "suspicious_pdf":
            return InvalidPDF("preview", message=message, action=action)
        case "browser_unavailable":
            return BrowserError("unavailable", message=message, action=action)
        case "navigate_failed":
            return BrowserError("navigate", message=message, action=action)
        case "internal_error":
            return InternalError(message=message, action=action)
        case "no_pdf_found":
            return NotFound("pdf", message=message, action=action)
        case "not_found":
            return NotFound(message=message, action=action)
        case _:
            return UnknownLegacyError(code, message=message, action=action)


def call_legacy(
    label: str,
    handler: LegacyHandler,
    identifier: str,
    path: Path,
    config: dict[str, Any],
) -> ProviderOutcome:
    # Only borrowed publisher browsers report a miss through a thread-local slot.
    diagnostics = None
    if label.endswith("Browser") and label != "InstitutionalBrowser":
        from scansci_pdf import _publisher_strategies_core

        diagnostics = _publisher_strategies_core
        diagnostics._clear_error()
    try:
        outcome = handler(identifier, path, dict(config))
    except UpstreamRateLimited as exc:
        return RateLimited.from_upstream(exc)
    except (
        httpx.RequestError,
        requests.RequestException,
        TimeoutError,
        ConnectionError,
    ) as exc:
        return NetworkError(message=str(exc))
    except Exception as exc:  # noqa: BLE001 - site failures must not prevent the next source
        return InternalError(message=str(exc))

    if outcome is None and diagnostics is not None:
        code, action = diagnostics.get_last_error()
        return _legacy_error(code, action=action) if code else NotFound()
    if not isinstance(outcome, dict):
        return NotFound()
    if outcome.get("success") is not False:
        candidate = Path(outcome.get("file") or outcome.get("path") or path)
        if is_readable_pdf(candidate):
            return SourceSuccess(
                path=candidate,
                source=outcome.get("source") or label,
                url=outcome.get("url"),
                metadata=outcome.get("metadata") or {},
            )
    return _legacy_error(
        str(outcome.get("error_type") or outcome.get("reason") or "not_found"),
        message=str(
            outcome.get("error")
            or outcome.get("message")
            or (outcome.get("reason") if outcome.get("error_type") else "")
            or ""
        ),
        action=str(outcome.get("action") or ""),
    )
