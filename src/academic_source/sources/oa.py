"""Progressive OA discovery: resolve public locations once, then verify actual PDFs."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from xml.etree.ElementTree import ParseError

import requests

from scansci_pdf.network import fetch, polite_delay
from scansci_pdf.pdf_utils import claim_download_url
from scansci_pdf.pmc_cloud import europepmc_pdf_url, find_article, lookup_pmcid
from scansci_pdf.upstream_client import openalex_params
from scansci_pdf.upstream_errors import RateLimited as UpstreamRateLimited

from ..domain import Attempt
from ..infrastructure.documents import is_readable_pdf
from .errors import (
    Blocked,
    ConfigNeeded,
    InvalidPDF,
    NetworkError,
    NotFound,
    RateLimited,
    SourceError,
)
from .models import (
    ProviderOutcome,
    SourceFailure,
    SourceSuccess,
    failed_attempt,
    observe,
)
from .oa_candidates import (
    Candidate,
    expand_candidate,
    is_preprint_doi,
    openalex_candidates,
    unpaywall_candidates,
)
from .registry import PROVIDERS

# Metadata lookups return small JSON; a slow one should not hold the whole chain.
OA_METADATA_READ_TIMEOUT = 8


def _metadata_config(config: dict[str, Any]) -> dict[str, Any]:
    """Apply the metadata read timeout; PDF downloads keep read_timeout."""
    return {
        **config,
        "read_timeout": config.get(
            "oa_metadata_read_timeout", OA_METADATA_READ_TIMEOUT
        ),
    }


@dataclass(frozen=True)
class Discovery:
    source: str
    candidates: list[Candidate]
    error: SourceError | None = None
    open_access: bool | None = None


def _metadata_http_error(status: int) -> SourceError:
    if status == 404:
        return NotFound(message="DOI not indexed by this service")
    message = f"Metadata HTTP {status}"
    if status == 429:
        return RateLimited(message=message, action="retry_later")
    return NetworkError(status, message=message, action="retry_later")


def _metadata(url: str, config: dict[str, Any]) -> tuple[dict, SourceError | None]:
    response = None
    try:
        response = fetch(
            url, _metadata_config(config), headers={"Accept": "application/json"}
        )
        status = response.status_code
        if status >= 400:
            return {}, _metadata_http_error(status)
        return response.json(), None
    except UpstreamRateLimited as exc:
        return {}, RateLimited.from_upstream(exc)
    except (
        requests.RequestException,
        TimeoutError,
        ConnectionError,
        ValueError,
    ) as exc:
        return {}, NetworkError(
            message=f"Metadata request: {type(exc).__name__}", action="retry_later"
        )
    finally:
        if response is not None:
            response.close()


def discover(doi: str, config: dict[str, Any]) -> Iterator[Discovery]:
    """Yield one service's candidates at a time; the caller may stop before the next query."""
    from scansci_pdf.config import DEFAULT_CONFIG
    from scansci_pdf.sources.unpaywall import _is_placeholder_email

    q = quote(doi, safe="")
    email = str(config.get("email") or DEFAULT_CONFIG["email"]).strip()
    if _is_placeholder_email(email):
        yield Discovery(
            "Unpaywall",
            [],
            ConfigNeeded(
                message="Unpaywall needs the user's real email address",
                action="ask_user_email",
            ),
        )
    else:
        payload, error = _metadata(
            f"https://api.unpaywall.org/v2/{q}?{urlencode({'email': email})}", config
        )
        if isinstance(error, NetworkError) and error.status_code == 422:
            error = ConfigNeeded(
                message="Unpaywall rejected the configured email address",
                action="ask_user_email",
            )
        verdict = payload.get("is_oa")
        yield Discovery(
            "Unpaywall",
            unpaywall_candidates(payload),
            error,
            verdict if isinstance(verdict, bool) else None,
        )

    auth = openalex_params(config, {"mailto": email} if email else {})
    params = f"?{urlencode(auth)}" if auth else ""
    payload, error = _metadata(
        f"https://api.openalex.org/works/doi:{q}{params}", config
    )
    verdict = (payload.get("open_access") or {}).get("is_oa")
    yield Discovery(
        "OpenAlexOA",
        openalex_candidates(payload, str(config.get("openalex_api_key") or "")),
        error,
        verdict if isinstance(verdict, bool) else None,
    )

    metadata_fetch = partial(fetch, config=_metadata_config(config))
    pmcid = None
    pdf = None
    error = None
    try:
        pmcid = lookup_pmcid(
            doi, partial(metadata_fetch, headers={"Accept": "application/json"})
        )
        if pmcid:
            article = find_article(pmcid, metadata_fetch)
            pdf = article.pdf if article else None
    except UpstreamRateLimited as exc:
        error = RateLimited.from_upstream(exc)
    except requests.HTTPError as exc:
        if exc.response is None:
            error = NetworkError(
                message="PMC metadata request failed", action="retry_later"
            )
        else:
            error = _metadata_http_error(exc.response.status_code)
    except (
        requests.RequestException,
        TimeoutError,
        ConnectionError,
        ParseError,
    ) as exc:
        error = NetworkError(
            message=f"Metadata request: {type(exc).__name__}", action="retry_later"
        )
    yield Discovery("PMC", [Candidate(pdf.url, "PMC")] if pdf else [], error)
    # Author manuscripts and temporary bucket failures can still use getPdf.
    if pmcid or error is None:
        yield Discovery(
            "EuropePMC",
            [Candidate(europepmc_pdf_url(pmcid), "EuropePMC")] if pmcid else [],
        )


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


def _http_candidate(
    candidate: Candidate, path: Path, config: dict[str, Any]
) -> tuple[ProviderOutcome, list[Candidate]]:
    public_url = _public_candidate_url(candidate)
    response = None
    partial = path.with_suffix(".pdf.part")
    try:
        polite_delay(config)
        response = fetch(candidate.url, config, stream=True)
        status = response.status_code
        if status == 404:
            return NotFound("pdf", message=f"PDF location HTTP 404: {public_url}"), []
        if status >= 400:
            message = f"HTTP {status}: {public_url}"
            if status in (401, 403, 503):
                return Blocked(
                    "http", status, message=message, action="retry_later"
                ), []
            if status == 429:
                return RateLimited(message=message, action="retry_later"), []
            return NetworkError(status, message=message, action="retry_later"), []
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
                    [],
                )
            return InvalidPDF(message=f"Unreadable PDF: {public_url}"), []

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
            return Blocked(
                "challenge",
                message=f"HTML challenge: {public_url}",
                action="check_browser_session",
            ), []
        links = _html_candidates(html, response.url or candidate.url, candidate.source)
        return NotFound("pdf", message=f"HTML/non-PDF response: {public_url}"), links
    except UpstreamRateLimited as exc:
        return RateLimited.from_upstream(exc), []
    except (
        requests.RequestException,
        TimeoutError,
        ConnectionError,
    ) as exc:
        return NetworkError(
            message=f"{type(exc).__name__}: {public_url}", action="retry_later"
        ), []
    finally:
        partial.unlink(missing_ok=True)
        if response is not None:
            response.close()


def _browser_candidate(
    candidate: Candidate, path: Path, config: dict[str, Any]
) -> ProviderOutcome:
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
            return NetworkError(
                message="CDP candidate navigation failed",
                action="check_browser_session",
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
                )
        return Blocked(
            message="CDP did not return a readable PDF", action="check_browser_session"
        )
    except Exception as exc:  # noqa: BLE001 - optional CDP failure must preserve HTTP fallback
        return NetworkError(
            message=f"CDP unavailable: {type(exc).__name__}",
            action="check_browser_session",
        )
    finally:
        if tab:
            browser.close_tab(tab, browser_config)


def acquire_oa(
    doi: str,
    work_dir: Path,
    config: dict[str, Any],
    *,
    run_browser: Callable[[str, Callable[[], ProviderOutcome]], ProviderOutcome]
    | None = None,
) -> SourceSuccess | SourceFailure:
    config = dict(config)
    config.setdefault("_acquisition_attempted_urls", set())
    attempts: list[Attempt] = []
    errors: list[SourceError] = []

    def record(source: str, result: ProviderOutcome, started: float) -> None:
        if isinstance(result, SourceSuccess):
            attempt = Attempt(source=source, status="succeeded")
        else:
            errors.append(result)
            attempt = failed_attempt(source, result)
        attempts.append(observe(doi, attempt, started))

    blocked: list[Candidate] = []
    preprints: list[Candidate] = []
    # Asking for a preprint DOI means the preprint is the wanted version.
    hold_preprints = not is_preprint_doi(doi)
    located = 0
    verdicts: dict[str, bool] = {}
    discovery = discover(doi, config)
    while True:
        # Resuming discovery may query metadata for the next candidates.
        started = time.monotonic()
        step = next(discovery, None)
        if step is None:
            break
        located += len(step.candidates)
        if step.open_access is not None:
            verdicts[step.source] = step.open_access
        if step.error is not None:
            record(step.source, step.error, started)
        else:
            attempt = Attempt(
                source=step.source,
                # The service answered either way; only "located" has addresses to try.
                status="located" if step.candidates else "no_open_copy",
                candidates=len(step.candidates),
                open_access=step.open_access,
            )
            attempts.append(observe(doi, attempt, started))
        queue = [
            (item, 0)
            for candidate in step.candidates
            for item in expand_candidate(candidate)
        ]
        for candidate, depth in queue:
            if urlsplit(candidate.url).scheme not in {"http", "https"}:
                continue
            if hold_preprints and candidate.preprint:
                if candidate not in preprints:
                    preprints.append(candidate)
                continue
            if not claim_download_url(candidate.url, config):
                continue
            path = work_dir / f"oa-{len(attempts)}.pdf"
            started = time.monotonic()
            result, links = _http_candidate(candidate, path, config)
            record(candidate.source, result, started)
            if isinstance(result, SourceSuccess):
                result.attempts = attempts
                return result
            if isinstance(result, Blocked):
                blocked.append(candidate)
            # Follow declared PDF links once, not an unbounded HTML crawl.
            if depth == 0:
                queue.extend(
                    (replace(item, preprint=item.preprint or candidate.preprint), 1)
                    for link in links[:3]
                    for item in expand_candidate(link)
                )
    if PROVIDERS["OACDP"].is_enabled(doi, config):

        def retry(candidate: Candidate) -> ProviderOutcome:
            started = time.monotonic()
            result = _browser_candidate(
                candidate, work_dir / f"oa-{len(attempts)}.pdf", config
            )
            record("OACDP", result, started)
            return result

        for candidate in blocked[:2]:
            result = (
                run_browser(
                    urlsplit(candidate.url).hostname or "", lambda c=candidate: retry(c)
                )
                if run_browser is not None
                else retry(candidate)
            )
            if isinstance(result, SourceSuccess):
                result.attempts = attempts
                return result
    # Conservative: one service alone is not enough, and any location overrides both.
    closed = (
        located == 0
        and verdicts.get("Unpaywall") is False
        and verdicts.get("OpenAlexOA") is False
    )
    return SourceFailure(
        reason="not_found",
        attempts=attempts,
        closed_access=closed,
        errors=errors,
        preprints=preprints,
    )


PREPRINT_WARNING = (
    "Only a preprint could be fetched: this PDF is the version submitted "
    "before peer review, not the published article."
)


def acquire_preprint(
    doi: str, candidates: list[Candidate], work_dir: Path, config: dict[str, Any]
) -> SourceSuccess | SourceFailure:
    """Try the preprint locations held back by acquire_oa, as a last resort."""
    attempts: list[Attempt] = []
    errors: list[SourceError] = []
    for candidate in candidates:
        if not claim_download_url(candidate.url, config):
            continue
        started = time.monotonic()
        result, _ = _http_candidate(
            candidate, work_dir / f"preprint-{len(attempts)}.pdf", config
        )
        if isinstance(result, SourceSuccess):
            attempts.append(
                observe(
                    doi, Attempt(source=candidate.source, status="succeeded"), started
                )
            )
            result.attempts = attempts
            result.warnings.append(PREPRINT_WARNING)
            return result
        errors.append(result)
        attempts.append(observe(doi, failed_attempt(candidate.source, result), started))
    return SourceFailure(reason="not_found", attempts=attempts, errors=errors)
