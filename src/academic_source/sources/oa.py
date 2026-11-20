"""Progressive OA discovery: resolve public locations once, then verify actual PDFs."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from xml.etree.ElementTree import ParseError

import requests

from scansci_pdf.network import fetch, polite_delay
from scansci_pdf.pdf_utils import claim_download_url
from scansci_pdf.pmc_cloud import (
    CloudArticle,
    europepmc_pdf_url,
    find_article,
    lookup_pmcid,
)
from scansci_pdf.upstream_client import openalex_params
from scansci_pdf.upstream_errors import RateLimited as UpstreamRateLimited

from ..domain import Attempt
from ..infrastructure.documents import is_readable_pdf, looks_like_preprint
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
    pmcid_from_location,
    unpaywall_candidates,
)

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
    found = openalex_candidates(payload, str(config.get("openalex_api_key") or ""))
    # OpenAlex's archived copy costs budget and does not say which version it is,
    # so free sources with a known version (PMC, getPdf) go first.
    archived = [item for item in found if item.source == "OpenAlexContent"]
    yield Discovery(
        "OpenAlexOA",
        [item for item in found if item.source != "OpenAlexContent"],
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
            article = _pmc_article(pmcid, config)
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
    if archived:
        yield Discovery("OpenAlexContent", archived)


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


def _pmc_article(pmcid: str, config: dict[str, Any]) -> CloudArticle | None:
    """List a PMCID in the bucket once per acquisition; errors are not remembered."""
    listed = config.setdefault("_pmc_articles", {})
    if pmcid not in listed:
        listed[pmcid] = find_article(
            pmcid, partial(fetch, config=_metadata_config(config))
        )
    return listed[pmcid]


def _expand(candidate: Candidate, config: dict[str, Any]) -> list[Candidate]:
    """A PMC location from Unpaywall or OpenAlex is tried from the bucket first.

    The bucket holds the same PDF as Europe PMC's getPdf and is typically twice
    as fast; author manuscripts are not in it, and getPdf still covers them.
    """
    expanded = expand_candidate(candidate)
    pmcid = pmcid_from_location(candidate.url)
    if not pmcid or candidate.source in {"PMC", "EuropePMC"}:
        return expanded
    try:
        article = _pmc_article(pmcid, config)
    except (requests.RequestException, UpstreamRateLimited, ParseError, ValueError):
        return expanded
    if not article or not article.pdf:
        return expanded
    return [Candidate(article.pdf.url, "PMC", candidate.landing_url), *expanded]


def acquire_oa(
    doi: str,
    work_dir: Path,
    config: dict[str, Any],
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

    preprints: list[Candidate] = []
    held: SourceSuccess | None = None
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
        # Expanded lazily: a PMC location costs a bucket listing only when reached.
        queue = [(candidate, 0) for candidate in step.candidates]
        if not hold_preprints:
            queue.sort(key=lambda entry: not entry[0].preprint)
        for raw, depth in queue:
            for candidate in _expand(raw, config):
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
                if (
                    hold_preprints
                    and isinstance(result, SourceSuccess)
                    and looks_like_preprint(result.path)
                ):
                    # An unlabelled copy that is a preprint waits like a preprint location.
                    attempt = Attempt(source=candidate.source, status="preprint")
                    attempts.append(observe(doi, attempt, started))
                    held = held or result
                    continue
                record(candidate.source, result, started)
                if isinstance(result, SourceSuccess):
                    result.attempts = attempts
                    return result
                # Follow declared PDF links once, not an unbounded HTML crawl.
                if depth == 0:
                    queue.extend(
                        (replace(link, preprint=link.preprint or candidate.preprint), 1)
                        for link in links[:3]
                    )
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
        held_preprint=held,
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
