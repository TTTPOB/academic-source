"""Science signed reader transport and clearance handling."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger()

_SCIENCE_DOI_PREFIX = "10.1126/"
_SCIENCE_EPDF_URL = "https://www.science.org/doi/epdf/{doi}"


def _science_signed_pdf_path(value: Any) -> str | None:
    """Validate a site-relative signed pdfdirect path.

    Only a path the publisher itself emitted is accepted; the signature is a
    short-lived credential and is never computed, extended, or rewritten here.
    """
    if not isinstance(value, str):
        return None
    candidate = value.replace("\\u003d", "=").replace("\\/", "/")
    if not candidate.startswith("/doi/pdfdirect/") or "hmac=" not in candidate:
        return None
    return candidate


def _science_reader_signed_url(tab_id: str, config: dict[str, Any]) -> str | None:
    """Read the server-signed pdfdirect URL from a loaded Science ePDF page.

    Science renders readerConfig.epubConfig.epubUrl into the ePDF HTML with a
    per-request hmac signature. The value is a short-lived credential: it is
    consumed in place and never logged or persisted.
    """
    from .browser_engine import evaluate_js

    return _science_signed_pdf_path(
        evaluate_js(
            tab_id,
            "(() => { const c = window.readerConfig;"
            " return (c && c.epubConfig && c.epubConfig.epubUrl) || null; })()",
            config,
        )
    )


def _wait_for_science_reader(
    tab_id: str, config: dict[str, Any], *, timeout: float
) -> str | None:
    """Poll until the Science reader replaces a Cloudflare challenge document.

    A challenge page and the reader page are different documents, so a single
    HTML sample can still be the challenge. Polling the reader config is the
    reliable completion signal.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        signed = _science_reader_signed_url(tab_id, config)
        if signed:
            return signed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(2.0, remaining))


_SCIENCE_READER_HTML_TIMEOUT = 30.0
_SCIENCE_PDF_TIMEOUT = 180.0
_SCIENCE_EPUB_URL_RE = re.compile(r'"epubUrl"\s*:\s*"([^"]+)"')


def _science_http_session(config: dict[str, Any], state: dict[str, Any]) -> Any:
    """Build a plain HTTP session carrying the cached Science clearance.

    Cloudflare binds cf_clearance to the user agent that earned it, so the
    captured agent is reused verbatim instead of the generic HTTP one.
    """
    import requests

    from .browser_backend import BACKEND_CDP, resolve_backend
    from .network import proxy_dict, science_http_proxy

    session = requests.Session()
    session.trust_env = False
    proxy = science_http_proxy(config, cdp=resolve_backend(config) == BACKEND_CDP)
    session.proxies = proxy_dict(proxy) or {}
    session.headers["User-Agent"] = state["user_agent"]
    for cookie in state["cookies"]:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie["domain"],
            path=cookie.get("path", "/"),
            secure=cookie.get("secure", False),
            expires=(
                int(cookie["expires"])
                if cookie.get("expires") not in (None, 0, -1)
                else None
            ),
        )
    return session


def _science_signed_url_over_http(
    doi: str, config: dict[str, Any], session: Any
) -> str | None:
    """Resolve the signed pdfdirect path from the ePDF HTML without a browser."""
    response = session.get(
        _SCIENCE_EPDF_URL.format(doi=doi),
        timeout=_SCIENCE_READER_HTML_TIMEOUT,
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    try:
        if str(response.headers.get("cf-mitigated", "")).lower() == "challenge":
            log.info("   [Science] HTTP reader challenge")
            return None
        if response.status_code != 200:
            log.info("   [Science] HTTP reader status=%s", response.status_code)
            return None
        match = _SCIENCE_EPUB_URL_RE.search(response.text)
        if not match:
            log.info("   [Science] HTTP reader has no epub URL")
            return None
        signed = _science_signed_pdf_path(match.group(1))
        if not signed:
            log.info("   [Science] HTTP reader unsupported epub URL")
        return signed
    finally:
        response.close()


def _science_http_download(
    doi: str, output_path: Path, config: dict[str, Any], state: dict[str, Any]
) -> bool:
    """Plain-HTTP Science path: ePDF HTML, then the signed pdfdirect resource.

    No browser page is opened. This only works while the cached clearance cookie
    and its issuing user agent are still accepted, so any failure simply defers
    to the browser strategy.
    """
    from urllib.parse import urljoin

    from .pdf_utils import _response_looks_pdf, is_pdf_file
    from .sources.publishers import _write_pdf_atomic

    session = None
    try:
        session = _science_http_session(config, state)
        signed = _science_signed_url_over_http(doi, config, session)
        if not signed:
            return False
        response = session.get(
            urljoin(_SCIENCE_EPDF_URL.format(doi=doi), signed),
            timeout=float(config.get("science_http_timeout", _SCIENCE_PDF_TIMEOUT)),
            stream=True,
            headers={"Accept": "application/pdf,*/*"},
        )
        try:
            if response.status_code >= 400:
                log.info("   [Science] HTTP PDF status=%s", response.status_code)
                return False
            iterator = response.iter_content(chunk_size=8192)
            first = next(iterator, b"")
            if not _response_looks_pdf(response, first):
                log.info("   [Science] HTTP PDF response is not a PDF")
                return False
            if not _write_pdf_atomic(output_path, first, iterator):
                log.info("   [Science] HTTP PDF write failed")
                return False
            if not is_pdf_file(output_path):
                log.info("   [Science] HTTP PDF validation failed")
                return False
            return True
        finally:
            response.close()
    except Exception as exc:
        log.info("   [Science] plain HTTP path failed: %s", type(exc).__name__)
        return False
    finally:
        if session is not None:
            session.close()


def _capture_science_clearance(tab_id: str, config: dict[str, Any]) -> None:
    """Persist the Science clearance cookie and its user agent for reuse.

    Later acquisitions can then resolve the signed URL over plain HTTP without
    opening a browser page. Failures here must never fail the acquisition.
    """
    try:
        from .browser_cookies import merge_cookies, save_science_http_state
        from .browser_engine import context_cookies, evaluate_js

        agent = evaluate_js(tab_id, "navigator.userAgent", config)
        cookies = context_cookies("https://www.science.org/", config)
        if isinstance(agent, str) and agent and cookies:
            save_science_http_state(agent, cookies, config)
            merge_cookies(cookies, config)
    except Exception as exc:
        log.info(f"   [Science] clearance capture skipped: {type(exc).__name__}")
