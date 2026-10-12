"""Progressive OA discovery: resolve public locations once, then verify actual PDFs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import requests

from scansci_pdf.network import fetch, polite_delay
from scansci_pdf.pdf_utils import claim_download_url
from scansci_pdf.upstream_client import openalex_params
from scansci_pdf.upstream_errors import RateLimited

from ..domain import Attempt
from ..infrastructure.documents import is_readable_pdf
from .models import SourceFailure, SourceSuccess
from .oa_candidates import (
    Candidate,
    europepmc_candidates,
    expand_candidate,
    openalex_candidates,
    pmc_candidates,
    unpaywall_candidates,
)

FAST_POLICIES = {"fastest", "oa_first", "legal_only"}
REPLACED_SOURCES = {
    "Unpaywall",
    "OpenAlexOA",
    "OpenAlexContent",
    "EuropePMC",
    "PMC",
}


def _failed(source: str, reason: str, message: str, action: str = "") -> Attempt:
    return Attempt(
        source=source, status="failed", reason=reason, message=message, action=action
    )


def _metadata(source: str, url: str, config: dict[str, Any]) -> tuple[dict, Attempt]:
    response = None
    try:
        response = fetch(url, config, headers={"Accept": "application/json"})
        status = response.status_code
        if status == 404:
            return {}, _failed(source, "not_found", "DOI not indexed by this service")
        if status >= 400:
            reason = "rate_limited" if status == 429 else "network_error"
            return {}, _failed(source, reason, f"Metadata HTTP {status}", "retry_later")
        payload = response.json()
        return payload, Attempt(source=source, status="discovered")
    except RateLimited as exc:
        return {}, Attempt(
            source=source,
            status="failed",
            reason="rate_limited",
            message=str(exc),
            action="retry_later",
            rate_limit=exc.to_dict(),
        )
    except (
        requests.RequestException,
        TimeoutError,
        ConnectionError,
        ValueError,
    ) as exc:
        return {}, _failed(
            source,
            "network_error",
            f"Metadata request: {type(exc).__name__}",
            "retry_later",
        )
    finally:
        if response is not None:
            response.close()


def discover(doi: str, config: dict[str, Any]):
    """Yield one service's candidates at a time; the caller may stop before the next query."""
    from scansci_pdf.config import DEFAULT_CONFIG
    from scansci_pdf.sources.unpaywall import _is_placeholder_email

    q = quote(doi, safe="")
    email = str(config.get("email") or DEFAULT_CONFIG["email"]).strip()
    if _is_placeholder_email(email):
        yield (
            [],
            _failed(
                "Unpaywall",
                "config_needed",
                "Unpaywall needs the user's real email address",
                "ask_user_email",
            ),
        )
    else:
        payload, attempt = _metadata(
            "Unpaywall",
            f"https://api.unpaywall.org/v2/{q}?{urlencode({'email': email})}",
            config,
        )
        if attempt.message == "Metadata HTTP 422":
            attempt = _failed(
                "Unpaywall",
                "config_needed",
                "Unpaywall rejected the configured email address",
                "ask_user_email",
            )
        yield unpaywall_candidates(payload), attempt

    auth = openalex_params(config, {"mailto": email} if email else {})
    params = f"?{urlencode(auth)}" if auth else ""
    payload, attempt = _metadata(
        "OpenAlexOA", f"https://api.openalex.org/works/doi:{q}{params}", config
    )
    yield (
        openalex_candidates(payload, str(config.get("openalex_api_key") or "")),
        attempt,
    )

    params = urlencode(
        {"query": f"DOI:{doi}", "format": "json", "pageSize": 5, "resultType": "core"}
    )
    payload, attempt = _metadata(
        "EuropePMC",
        f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{params}",
        config,
    )
    yield europepmc_candidates(payload), attempt

    params = urlencode({"ids": doi, "format": "json"})
    payload, attempt = _metadata(
        "PMC", f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?{params}", config
    )
    yield pmc_candidates(payload), attempt


def _html_candidates(html: str, base_url: str, source: str) -> list[Candidate]:
    from html import unescape
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    from scansci_pdf.sources.crossref import _extract_pdf_urls

    soup = BeautifulSoup(html, "html.parser")
    declared = [
        tag.get("content") or tag.get("href")
        for tag in soup.select(
            'meta[name="citation_pdf_url"], link[type="application/pdf"]'
        )
    ]
    urls = [urljoin(base_url, url) for url in declared if url]
    urls.extend(unescape(url) for url in _extract_pdf_urls(html, base_url))
    return [Candidate(url, source, base_url) for url in dict.fromkeys(urls)]


def _public_candidate_url(candidate: Candidate, resolved_url: str | None = None) -> str:
    """Use a credential-free Content endpoint as provenance across download paths."""
    if candidate.source == "OpenAlexContent":
        return urlsplit(candidate.url)._replace(query="", fragment="").geturl()
    return resolved_url if resolved_url is not None else candidate.url


def _http_candidate(candidate: Candidate, path: Path, config: dict[str, Any]):
    public_url = _public_candidate_url(candidate)
    response = None
    partial = path.with_suffix(".pdf.part")
    try:
        polite_delay(config)
        response = fetch(candidate.url, config, stream=True)
        status = response.status_code
        if status == 404:
            return (
                None,
                _failed(
                    candidate.source,
                    "no_pdf_found",
                    f"PDF location HTTP 404: {public_url}",
                ),
                [],
            )
        if status >= 400:
            reason = (
                "blocked"
                if status in (401, 403, 503)
                else "rate_limited"
                if status == 429
                else "network_error"
            )
            return (
                None,
                _failed(
                    candidate.source,
                    reason,
                    f"HTTP {status}: {public_url}",
                    "retry_later",
                ),
                [],
            )
        chunks = response.iter_content(chunk_size=8192)
        first = next(chunks, b"")
        if first.startswith(b"%PDF-"):
            deadline = time.monotonic() + float(
                config.get("download_deadline_seconds", 60)
            )
            with partial.open("wb") as stream:
                stream.write(first)
                for chunk in chunks:
                    if time.monotonic() > deadline:
                        raise TimeoutError("stream deadline exceeded")
                    stream.write(chunk)
            if is_readable_pdf(partial):
                partial.replace(path)
                return (
                    SourceSuccess(path=path, source=candidate.source, url=public_url),
                    Attempt(source=candidate.source, status="succeeded"),
                    [],
                )
            return (
                None,
                _failed(
                    candidate.source, "invalid_pdf", f"Unreadable PDF: {public_url}"
                ),
                [],
            )

        # Landing pages are useful discovery inputs; challenges are never PDFs.
        body = bytearray(first)
        for chunk in chunks:
            body.extend(chunk)
            if len(body) >= 1024 * 1024:
                break
        html = body.decode(response.encoding or "utf-8", errors="replace")
        lower = html.lower()
        if any(
            marker in lower
            for marker in (
                "recaptcha",
                "hcaptcha",
                "cf-browser-verification",
                "just a moment",
                "checking your browser",
                "verify you are human",
                "challenge-platform",
            )
        ):
            return (
                None,
                _failed(
                    candidate.source,
                    "blocked",
                    f"HTML challenge: {public_url}",
                    "check_browser_session",
                ),
                [],
            )
        links = _html_candidates(html, response.url or candidate.url, candidate.source)
        return (
            None,
            _failed(
                candidate.source,
                "no_pdf_found",
                f"HTML/non-PDF response: {public_url}",
            ),
            links,
        )
    except RateLimited as exc:
        return (
            None,
            Attempt(
                source=candidate.source,
                status="failed",
                reason="rate_limited",
                message=str(exc),
                action="retry_later",
                rate_limit=exc.to_dict(),
            ),
            [],
        )
    except (
        requests.RequestException,
        TimeoutError,
        ConnectionError,
    ) as exc:
        return (
            None,
            _failed(
                candidate.source,
                "network_error",
                f"{type(exc).__name__}: {public_url}",
                "retry_later",
            ),
            [],
        )
    finally:
        partial.unlink(missing_ok=True)
        if response is not None:
            response.close()


def _browser_candidate(candidate: Candidate, path: Path, config: dict[str, Any]):
    """Borrow a CDP tab for one bounded, same-origin retry; never restart the browser."""
    from scansci_pdf import browser_engine as browser

    tab = None
    browser_config = dict(config)
    browser_config["browser_pdf_timeout"] = min(
        float(config.get("browser_pdf_timeout", 120)), 20
    )
    origin = urlsplit(candidate.url)
    landing = candidate.landing_url
    if not landing or urlsplit(landing).netloc != origin.netloc:
        landing = f"{origin.scheme}://{origin.netloc}/"
    try:
        tab = browser.create_tab("about:blank", browser_config, timeout=10)
        if not tab or not browser.navigate_tab(
            tab, landing, browser_config, timeout=10
        ):
            return None, _failed(
                "OACDP",
                "network_error",
                "CDP candidate navigation failed",
                "check_browser_session",
            )
        snapshot = browser.get_snapshot(tab, browser_config)
        links = _html_candidates(
            snapshot.get("snapshot", ""),
            snapshot.get("url") or landing,
            candidate.source,
        )
        for url in dict.fromkeys([candidate.url, *(item.url for item in links[:1])]):
            if browser.fetch_pdf_in_tab(
                tab, url, path, browser_config
            ) and is_readable_pdf(path):
                return SourceSuccess(
                    path=path,
                    source=candidate.source,
                    url=_public_candidate_url(candidate, url),
                ), Attempt(source="OACDP", status="succeeded")
        return None, _failed(
            "OACDP",
            "blocked",
            "CDP did not return a readable PDF",
            "check_browser_session",
        )
    except Exception as exc:  # noqa: BLE001 - optional CDP failure must preserve HTTP fallback
        return None, _failed(
            "OACDP",
            "network_error",
            f"CDP unavailable: {type(exc).__name__}",
            "check_browser_session",
        )
    finally:
        if tab:
            browser.close_tab(tab, browser_config)


def acquire_oa(
    doi: str, work_dir: Path, config: dict[str, Any]
) -> SourceSuccess | SourceFailure:
    config = dict(config)
    config.setdefault("_acquisition_attempted_urls", set())
    attempts: list[Attempt] = []
    blocked: list[Candidate] = []
    for candidates, discovery_attempt in discover(doi, config):
        attempts.append(discovery_attempt)
        queue = [
            (item, 0)
            for candidate in candidates
            for item in expand_candidate(candidate)
        ]
        for candidate, depth in queue:
            if urlsplit(candidate.url).scheme not in {"http", "https"}:
                continue
            if not claim_download_url(candidate.url, config):
                continue
            path = work_dir / f"oa-{len(attempts)}.pdf"
            result, attempt, links = _http_candidate(candidate, path, config)
            attempts.append(attempt)
            if result:
                result.attempts = attempts
                return result
            if attempt.reason == "blocked":
                blocked.append(candidate)
            # Follow declared PDF links once, not an unbounded HTML crawl.
            if depth == 0:
                queue.extend(
                    (item, 1) for link in links[:3] for item in expand_candidate(link)
                )
    if str(config.get("browser_backend", "")).lower() == "cdp":
        for candidate in blocked[:2]:
            result, attempt = _browser_candidate(
                candidate, work_dir / f"oa-{len(attempts)}.pdf", config
            )
            attempts.append(attempt)
            if result:
                result.attempts = attempts
                return result
    return SourceFailure(reason="not_found", attempts=attempts)
