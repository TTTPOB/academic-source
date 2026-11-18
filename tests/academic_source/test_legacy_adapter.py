"""Exercise each borrowed failure channel without provider I/O."""

import pytest
import requests

from academic_source.sources.errors import (
    Blocked,
    BrowserError,
    InternalError,
    NetworkError,
    NotFound,
    Paywall,
    RateLimited,
)
from academic_source.sources.legacy import call_legacy
from academic_source.sources.models import SourceFailure
from scansci_pdf import _publisher_strategies_core as browser
from scansci_pdf.upstream_errors import RateLimited as UpstreamRateLimited


@pytest.mark.parametrize(
    "exception,expected",
    [
        (requests.Timeout("timed out"), NetworkError),
        (ValueError("invalid"), InternalError),
        (UpstreamRateLimited("openalex", retry_at=42), RateLimited),
    ],
)
def test_legacy_exceptions(exception, expected, tmp_path):
    def handler(*args):
        raise exception

    error = call_legacy("Example", handler, "10.9999/test", tmp_path / "paper.pdf", {})
    assert isinstance(error, expected)
    assert error.message == str(exception)
    if isinstance(error, RateLimited):
        assert error.details() == exception.to_dict()
        assert error.action == "retry_later"


def test_first_ranked_failure_wins(tmp_path):
    def handler(*args):
        return {
            "success": False,
            "error_type": "paywall",
            "reason": "Subscription needed",
            "action": "login_required",
        }

    error = call_legacy(
        "ElsevierAPI", handler, "10.1016/test", tmp_path / "paper.pdf", {}
    )
    assert error == Paywall(message="Subscription needed", action="login_required")
    summary = SourceFailure.summarize(
        [error, Paywall(message="later"), NetworkError()], []
    )
    assert (summary.reason, summary.message, summary.action) == (
        "auth_required",
        "Subscription needed",
        "login_required",
    )


def test_browser_reads_diagnostic_only_after_returning_none(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "_last_error_type", "paywall")
    monkeypatch.setattr(browser, "_last_error_action", "stale")

    def run(handler):
        return call_legacy(
            "ScienceBrowser", handler, "10.1126/test", tmp_path / "paper.pdf", {}
        )

    assert run(lambda *args: None) == NotFound()

    def blocked(*args):
        browser._set_error("cloudflare_blocked", "try_other_source")

    assert run(blocked) == Blocked("cloudflare", action="try_other_source")

    def unavailable(*args):
        blocked()
        return {"success": False, "reason": "browser_unavailable"}

    error = run(unavailable)
    assert error == BrowserError("unavailable")
    summary = SourceFailure.summarize([NetworkError(), error], [])
    assert summary.reason == "network_error"
    assert SourceFailure.summarize([error, NotFound()], []).reason == "unsupported"
