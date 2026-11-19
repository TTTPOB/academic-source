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


_SCIENCE_FETCH_TIMEOUT_MS = 8_000
_SCIENCE_EPUB_URL_RE = re.compile(r'"epubUrl"\s*:\s*"([^"]+)"')


def _science_fetch_download(
    doi: str, output_path: Path, config: dict[str, Any], *, tab_id: str | None = None
) -> bool:
    """Fetch reader HTML and PDF from an authenticated Science tab."""
    from urllib.parse import urlsplit

    from .browser_engine import (
        _resolve_tab,
        close_tab,
        create_tab,
        fetch_pdf_in_tab,
        get_owned_tab_for_origin,
        navigate_tab,
    )

    reader_url = _SCIENCE_EPDF_URL.format(doi=doi)
    origin = urlsplit(reader_url)
    own_tab = False
    try:
        if tab_id is None:
            tab_id = get_owned_tab_for_origin(f"{origin.scheme}://{origin.netloc}")
        if tab_id is None:
            own_tab = True
            tab_id = create_tab(reader_url, config, timeout=_SCIENCE_FETCH_TIMEOUT_MS / 1000)
        if tab_id is None or (page := _resolve_tab(tab_id)) is None:
            return False
        current = urlsplit(page.url)
        if not own_tab and (current.scheme, current.netloc) != (origin.scheme, origin.netloc):
            if not navigate_tab(
                tab_id, reader_url, config, timeout=_SCIENCE_FETCH_TIMEOUT_MS / 1000
            ):
                return False
            current = urlsplit(page.url)
        if (current.scheme, current.netloc) != (origin.scheme, origin.netloc):
            return False
        reader = page.evaluate(
            """async ({url, timeout}) => {
                const response = await fetch(url, {
                    credentials: 'same-origin', mode: 'same-origin', redirect: 'error',
                    headers: {'Accept': 'text/html,application/xhtml+xml'},
                    signal: AbortSignal.timeout(timeout)
                });
                const challenge = response.headers.get('cf-mitigated')?.toLowerCase() === 'challenge';
                return {status: response.status, challenge,
                        html: response.status === 200 && !challenge ? await response.text() : ''};
            }""",
            {"url": reader_url, "timeout": _SCIENCE_FETCH_TIMEOUT_MS},
        )
        if reader["challenge"] or reader["status"] != 200:
            log.info(
                "   [Science] in-page reader rejected (status=%s, challenge=%s)",
                reader["status"],
                reader["challenge"],
            )
            return False
        match = _SCIENCE_EPUB_URL_RE.search(reader["html"])
        signed = _science_signed_pdf_path(match.group(1)) if match else None
        if not signed or urlsplit(signed).path != f"/doi/pdfdirect/{doi}":
            return False
        return fetch_pdf_in_tab(tab_id, signed, output_path, config)
    except Exception as exc:
        # A Playwright exception can include request data. Keep the fallback log safe.
        log.info("   [Science] in-page fetch failed: %s", type(exc).__name__)
        return False
    finally:
        if own_tab and tab_id is not None:
            close_tab(tab_id, config)


def _remember_local_science_cookies(config: dict[str, Any]) -> None:
    """Preserve the generic cookie cache for the local browser fallback only."""
    from .browser_backend import BACKEND_CDP, resolve_backend

    if resolve_backend(config) == BACKEND_CDP:
        return
    try:
        from .browser_cookies import merge_cookies
        from .browser_engine import context_cookies

        cookies = context_cookies("https://www.science.org/", config)
        if cookies:
            merge_cookies(cookies, config)
    except Exception as exc:
        log.info("   [Science] local cookie cache skipped: %s", type(exc).__name__)
