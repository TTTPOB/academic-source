"""Discover and stream publisher supplementary files independently of the main PDF."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .sources.elsevier_api import get_api_key
from .upstream_errors import RateLimited

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 200_000_000
MAX_PAPER_BYTES = 600_000_000
_ELSEVIER_API_HOST = "api.elsevier.com"


@dataclass(frozen=True)
class SupplementaryItem:
    url: str
    label: str = ""
    description: str = ""
    filename: str = ""


@dataclass(frozen=True)
class SavedSupplementary:
    path: Path
    item: SupplementaryItem
    page_url: str


@dataclass(frozen=True)
class UnsavedSupplementary:
    item: SupplementaryItem
    reason: str


@dataclass
class SupplementaryResult:
    saved: list[SavedSupplementary] = field(default_factory=list)
    unsaved: list[UnsavedSupplementary] = field(default_factory=list)
    pages_read: int = 0
    pages_unread: int = 0
    source: str = "publisher"
    route: str = "publisher"
    pmcid: str = ""
    route_warnings: list[str] = field(default_factory=list)


_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|gz|csv|txt)(?:[?#]|$)", re.I)
_MARKER_RE = re.compile(
    r"(suppl|supp_|supporting|_si\d|/cms/|attachment|mmc\d|moesm)", re.I
)
_STRONG_RE = re.compile(r"(supplementary[-_]?file|/cms/|attachment|mmc\d)", re.I)
_MAIN_PDF_RE = re.compile(r"(?:^|/)(?:main|mainext|article)\.pdf(?:[?#]|$)", re.I)

_CT_EXT = {
    "image/tiff": ".tiff",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/avi": ".avi",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-powerpoint": ".ppt",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "text/plain": ".txt",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/json": ".json",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/x-tar": ".tar",
    "application/tar": ".tar",
}


def extract_supplementary_links(html: str, base_url: str) -> list[SupplementaryItem]:
    """Apply the generic URL heuristics and keep the publisher's link and caption text."""
    seen: set[str] = set()
    items: list[SupplementaryItem] = []
    for link in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = link["href"].strip()
        if (
            href.startswith("#")
            or not _MARKER_RE.search(href)
            or _MAIN_PDF_RE.search(href)
        ):
            continue
        if not _EXT_RE.search(href) and not _STRONG_RE.search(href):
            continue
        try:
            url, _ = urldefrag(urljoin(base_url, href))
            if urlsplit(url).scheme not in ("http", "https"):
                continue
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        container = link.find_parent(("li", "figcaption", "tr"))
        if container is None and (figure := link.find_parent("figure")) is not None:
            container = figure.find("figcaption")
        if container is None:
            container = link.find_parent("p")
        description = ""
        if container is not None:
            blocks = (
                container.find_all(("td", "th"), recursive=False)
                if container.name == "tr"
                else [container]
            )
            description = "\n".join(
                text
                for block in blocks
                if (
                    text := "".join(
                        str(part)
                        for part in block.strings
                        if all(parent is not link for parent in part.parents)
                    ).strip()
                )
            )
        items.append(
            SupplementaryItem(
                url,
                label=link.get_text().strip() or link.get("title", ""),
                description=description,
                filename=_url_filename(url),
            )
        )
    return items


def fetch_supplementary(
    doi: str,
    output_dir: str | Path,
    config: dict | None = None,
    timeout: int = 30,
    *,
    run_browser: Callable[[str, Callable[[], None]], None] | None = None,
) -> SupplementaryResult:
    """Read strategy pages in order and report the outcome for every distinct file URL."""
    from .publisher_strategies import StrategyRegistry

    config = config or {}
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    strategy = StrategyRegistry.get_for_doi(doi)
    result = SupplementaryResult()
    if strategy is not None and strategy.name not in {"", "generic"}:
        result.source = strategy.name
    saved_bytes = 0
    seen: set[str] = set()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", doi.strip()).strip("_")

    def process_page(
        html: str,
        page_url: str,
        session: requests.Session,
        *,
        api: bool = False,
        page: Any = None,
    ) -> bool:
        items = strategy.supplementary_items(html, page_url)
        if items is None:
            if api:
                return False
            items = extract_supplementary_links(html, page_url)
        save_items(items, page_url, session, page)
        return True

    def save_items(
        items: list[SupplementaryItem],
        page_url: str,
        session: requests.Session,
        page: Any,
    ) -> None:
        nonlocal saved_bytes
        result.pages_read += 1
        for item in items:
            url, _ = urldefrag(item.url)
            if url in seen:
                continue
            seen.add(url)
            remaining = MAX_PAPER_BYTES - saved_bytes
            if not remaining:
                result.unsaved.append(
                    UnsavedSupplementary(item, paper_size_limit_reason(MAX_PAPER_BYTES))
                )
                continue
            downloaded = _download(
                item,
                page_url,
                out / f"{stem}_SI{len(seen)}",
                session,
                config,
                timeout,
                remaining=remaining,
                page=page,
            )
            if isinstance(downloaded, SavedSupplementary):
                result.saved.append(downloaded)
                saved_bytes += downloaded.path.stat().st_size
            else:
                result.unsaved.append(downloaded)

    visited: set[str] = set()
    read: set[str] = set()

    def visit(page_url: str, session: requests.Session, *, follow_tab: bool) -> None:
        if page_url in visited or page_url in read:
            return
        visited.add(page_url)
        next_page = None

        def consume(
            html: str,
            final_url: str,
            attachment_session: requests.Session,
            page: Any = None,
        ) -> None:
            nonlocal next_page
            if final_url not in read:
                process_page(html, final_url, attachment_session, page=page)
                read.add(final_url)
            if follow_tab:
                next_page = _redirected_tab(page_url, final_url, html)

        def fallback() -> None:
            with _browser_page(page_url, config, timeout) as fetched:
                if fetched is None:
                    result.pages_unread += 1
                else:
                    consume(*fetched)

        fetched = _fetch_page(page_url, session, config, timeout)
        if fetched is not None:
            consume(*fetched, session)
        elif run_browser is None:
            fallback()
        else:
            run_browser(urlsplit(page_url).hostname or "", fallback)
        if next_page is not None:
            visit(next_page, session, follow_tab=False)

    with requests.Session() as session:
        session.trust_env = False
        session.headers["User-Agent"] = _user_agent()
        pmc = _fetch_europepmc(doi, out, session, config, timeout)
        if pmc.saved:
            return pmc
        if strategy is None:
            return pmc
        result.pages_read = pmc.pages_read
        result.pages_unread = pmc.pages_unread
        result.unsaved.extend(pmc.unsaved)
        result.route_warnings.extend(pmc.route_warnings)
        result.route_warnings.append(
            "Europe PMC did not provide supplementary files; tried the publisher route"
        )
        for page_url in strategy.supplementary_pages(doi):
            if not _elsevier_origin(page_url):
                visit(page_url, session, follow_tab=True)
                continue
            if not get_api_key(config.get("elsevier_api_key", "")):
                continue
            fetched = _fetch_page(page_url, session, config, timeout)
            if fetched is None:
                result.pages_unread += 1
            elif process_page(*fetched, session, api=True) and seen:
                result.route = "Elsevier API"
                break
    return result


def _fetch_europepmc(
    doi: str,
    out: Path,
    session: requests.Session,
    config: dict,
    timeout: int,
) -> SupplementaryResult:
    from .supplementary_europepmc import fetch_europepmc_supplementary

    return fetch_europepmc_supplementary(
        doi,
        out,
        lambda url: _request(url, session, config, timeout),
        max_file_bytes=MAX_FILE_BYTES,
        max_total_bytes=MAX_PAPER_BYTES,
    )


def _redirected_tab(requested_url: str, page_url: str, html: str) -> str | None:
    # Rxiv's unversioned supplementary tab redirects to the current article.
    # Follow only the matching, explicitly linked tab on that same origin.
    suffix = ".supplementary-material"
    if not urlsplit(requested_url).path.endswith(suffix):
        return None
    page = urlsplit(page_url)
    if page.path.endswith(suffix):
        return None
    for link in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        try:
            target, _ = urldefrag(urljoin(page_url, link["href"]))
            parts = urlsplit(target)
        except ValueError:
            continue
        if (parts.scheme, parts.netloc, parts.path) == (
            page.scheme,
            page.netloc,
            page.path + suffix,
        ):
            return target
    return None


def _elsevier_origin(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc.lower() in {
        _ELSEVIER_API_HOST,
        f"{_ELSEVIER_API_HOST}:443",
    }


def _request(
    url: str,
    session: requests.Session,
    config: dict,
    timeout: int,
    *,
    article: bool = False,
) -> requests.Response:
    proxy = config.get("network_proxy", "")
    options = dict(
        proxies={"http": proxy, "https": proxy} if proxy else None,
        timeout=timeout,
        stream=True,
    )

    def get(target: str, **kwargs) -> requests.Response:
        if config.get("_parallel_http"):
            from .upstream_client import request_response

            return request_response(
                target, config, session=session, **options, **kwargs
            )
        return session.get(target, **options, **kwargs)

    if not _elsevier_origin(url):
        return get(url)
    headers = {"Accept": "application/xml" if article else "*/*"}
    if key := get_api_key(config.get("elsevier_api_key", "")):
        headers["X-ELS-APIKey"] = key
        if token := config.get("elsevier_insttoken"):
            headers["X-ELS-Insttoken"] = token
    # Requests only strips standard Authorization on redirects. Do not ask it
    # (or the parallel client) to follow a credentialed hop automatically.
    for _ in range(session.max_redirects + 1):
        response = get(url, headers=headers, allow_redirects=False)
        if not response.is_redirect:
            return response
        with response:
            url = urljoin(response.url, response.headers["Location"])
        if not _elsevier_origin(url):
            return get(url)
    raise requests.TooManyRedirects("Elsevier API redirect limit exceeded")


def _fetch_page(
    url: str, session: requests.Session, config: dict, timeout: int
) -> tuple[str, str] | None:
    try:
        with _request(url, session, config, timeout, article=True) as response:
            if (
                response.status_code == 200
                and response.headers.get("cf-mitigated") != "challenge"
            ):
                html = response.text
                if _readable_page(html, response.url):
                    return html, response.url
    except (requests.RequestException, RateLimited):
        pass
    return None


def _is_challenge(html: str) -> bool:
    return "<title>just a moment" in html.lower() or "_cf_chl_opt" in html


def _readable_page(html: str, url: str) -> bool:
    return (
        bool(html.strip())
        and "linkinghub." not in (urlsplit(url).hostname or "")
        and not _is_challenge(html)
    )


def _browser_snapshot(page: Any) -> tuple[str, str]:
    try:
        return page.content(), page.url
    except Exception as exc:
        # A self-reloading challenge can replace the DOM while it is being read.
        message = str(exc).lower()
        if (
            "navigat" not in message
            and "execution context was destroyed" not in message
        ):
            raise
        return "", page.url


@contextmanager
def _browser_page(article_url: str, config: dict, timeout: int):
    """Keep the article tab and all attachment requests in the browser lane."""
    from .browser_engine import _close_page, get_browser_page

    page = None
    try:
        try:
            browser_config = {
                key: value
                for key, value in config.items()
                if key not in {"elsevier_api_key", "elsevier_insttoken"}
            }
            page = get_browser_page(browser_config)
            fetched = None
            if page is not None:
                response = page.goto(
                    article_url,
                    wait_until="domcontentloaded",
                    timeout=max(timeout, 45) * 1000,
                )
                page.wait_for_timeout(2500)
                html, final_url = _browser_snapshot(page)
                loaded = response is None or response.ok or response.url != final_url
                if _is_challenge(html) or (
                    response is not None
                    and not response.ok
                    and response.headers.get("cf-mitigated") == "challenge"
                ):
                    # A successful reload can leave the initial response's status stale.
                    remaining_ms = 27_500  # Include the initial page-settle delay.
                    while (
                        not (loaded := _readable_page(html, final_url)) and remaining_ms
                    ):
                        delay = min(1000, remaining_ms)
                        page.wait_for_timeout(delay)
                        remaining_ms -= delay
                        html, final_url = _browser_snapshot(page)
                if loaded and _readable_page(html, final_url):
                    fetched = (
                        html,
                        final_url,
                        page.evaluate("navigator.userAgent"),
                        page.context.cookies(),
                    )
        except Exception as exc:
            log.debug("Supplementary browser page unavailable (%s)", type(exc).__name__)
            fetched = None
        if fetched is None:
            yield None
            return
        html, final_url, user_agent, cookies = fetched
        with requests.Session() as session:
            # Cross-origin requests and browser failures use a separate streaming
            # session with the browser's domain/path-scoped cookies.
            session.trust_env = False
            session.headers["User-Agent"] = user_agent
            for cookie in cookies:
                session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie["domain"],
                    path=cookie["path"],
                    secure=cookie["secure"],
                    expires=int(cookie["expires"]) if cookie["expires"] > 0 else None,
                )
            yield html, final_url, session, page
    finally:
        if page is not None:
            try:
                _close_page(page)
            except Exception as exc:
                log.debug(
                    "Supplementary browser page close failed (%s)", type(exc).__name__
                )


def _download(
    item: SupplementaryItem,
    page_url: str,
    stem: Path,
    session: requests.Session,
    config: dict,
    timeout: int,
    *,
    remaining: int,
    page: Any = None,
) -> SavedSupplementary | UnsavedSupplementary:
    partial = stem.with_name(stem.name + ".part")
    try:
        browser_failure = None
        if page is not None:
            from .browser_engine import same_origin, stream_file_in_tab

            if same_origin(page.url, item.url):
                transfer = stream_file_in_tab(
                    page,
                    item.url,
                    partial,
                    timeout_ms=max(1, timeout * 1000),
                    max_bytes=min(MAX_FILE_BYTES, remaining),
                    max_declared_bytes=MAX_FILE_BYTES,
                )
                response = transfer.response
                browser_item, suffix = item, ""
                if response is not None:
                    browser_item, suffix = _download_name(
                        item, response.url, response.disposition
                    )
                if transfer.overflow_size:
                    size = transfer.overflow_size
                    reason = (
                        file_size_limit_reason(
                            size, MAX_FILE_BYTES, minimum=transfer.phase == "body"
                        )
                        if size > MAX_FILE_BYTES
                        else paper_size_limit_reason(MAX_PAPER_BYTES)
                    )
                    return UnsavedSupplementary(browser_item, reason)
                browser_failure = transfer.reason
                if not browser_failure and response is not None:
                    saved = _finish_download(
                        browser_item, page_url, stem, partial, suffix, response.mime
                    )
                    if isinstance(saved, SavedSupplementary):
                        return saved
                    browser_failure = saved.reason
                partial.unlink(missing_ok=True)

        downloaded = _download_http(
            item, page_url, stem, partial, session, config, timeout, remaining
        )
        if browser_failure and isinstance(downloaded, UnsavedSupplementary):
            return replace(
                downloaded,
                reason=f"in-page fetch: {browser_failure}; external HTTP: {downloaded.reason}",
            )
        return downloaded
    finally:
        partial.unlink(missing_ok=True)


def _download_name(
    item: SupplementaryItem, final_url: str, header: str
) -> tuple[SupplementaryItem, str]:
    disposition = Message()
    if header:
        disposition["Content-Disposition"] = header
    header_name = disposition.get_filename()
    if header_name == "f.txt":
        # Elsevier's object API sends this placeholder for every file type.
        header_name = None
    if header_name:
        header_name = header_name.replace("\\", "/").rsplit("/", 1)[-1]
    url_name = _url_filename(item.url) or _url_filename(final_url)
    filename = header_name or item.filename or url_name
    return replace(item, filename=filename), _extension(filename) or _extension(
        url_name
    )


def _finish_download(
    item: SupplementaryItem,
    page_url: str,
    stem: Path,
    partial: Path,
    suffix: str,
    ctype: str,
) -> SavedSupplementary | UnsavedSupplementary:
    try:
        with partial.open("rb") as stream:
            prefix = stream.read(1024)
        if reason := file_validation_error(prefix, suffix, content_type=ctype):
            return UnsavedSupplementary(item, reason)
        ext = suffix or _CT_EXT.get(ctype, ".bin")
        if not suffix and prefix.startswith(b"%PDF-"):
            ext = ".pdf"
        elif ext == ".pdf" and not prefix.startswith(b"%PDF-"):
            # An unnamed response may claim to be a PDF without being one.
            ext = ".bin"
        path = stem.with_name(stem.name + ext)
        partial.replace(path)
        return SavedSupplementary(path, item, page_url)
    except OSError:
        return UnsavedSupplementary(item, "could not write file")


def _download_http(
    item: SupplementaryItem,
    page_url: str,
    stem: Path,
    partial: Path,
    session: requests.Session,
    config: dict,
    timeout: int,
    remaining: int,
) -> SavedSupplementary | UnsavedSupplementary:
    try:
        with _request(item.url, session, config, timeout) as response:
            if response.headers.get("cf-mitigated") == "challenge":
                return UnsavedSupplementary(
                    item, f"HTTP {response.status_code} (browser challenge)"
                )
            if response.status_code != 200:
                return UnsavedSupplementary(item, f"HTTP {response.status_code}")
            ctype = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            item, suffix = _download_name(
                item, response.url, response.headers.get("content-disposition", "")
            )
            try:
                expected_size = int(response.headers.get("content-length", ""))
            except ValueError:
                expected_size = 0
            if expected_size > MAX_FILE_BYTES:
                return UnsavedSupplementary(
                    item, file_size_limit_reason(expected_size, MAX_FILE_BYTES)
                )
            identity = (
                response.headers.get("content-encoding", "identity").lower()
                == "identity"
            )
            if identity and expected_size > remaining:
                return UnsavedSupplementary(
                    item, paper_size_limit_reason(MAX_PAPER_BYTES)
                )
            size = 0
            with partial.open("wb") as stream:
                try:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            return UnsavedSupplementary(
                                item,
                                file_size_limit_reason(
                                    size, MAX_FILE_BYTES, minimum=True
                                ),
                            )
                        if size > remaining:
                            return UnsavedSupplementary(
                                item, paper_size_limit_reason(MAX_PAPER_BYTES)
                            )
                        stream.write(chunk)
                except requests.exceptions.ChunkedEncodingError:
                    return UnsavedSupplementary(item, "truncated transfer")
            if identity and size < expected_size:
                return UnsavedSupplementary(
                    item, f"truncated transfer ({size:,} of {expected_size:,} bytes)"
                )
        return _finish_download(item, page_url, stem, partial, suffix, ctype)
    except RateLimited as exc:
        return UnsavedSupplementary(item, f"HTTP {exc.status_code}")
    except requests.RequestException:
        return UnsavedSupplementary(item, "network error")
    except OSError:
        return UnsavedSupplementary(item, "could not write file")


_FILE_SIGNATURES = {
    ".pdf": b"%PDF-",
    ".zip": b"PK",
    ".docx": b"PK",
    ".xlsx": b"PK",
    ".pptx": b"PK",
    ".gz": b"\x1f\x8b",
}


def file_validation_error(
    prefix: bytes, extension: str, *, content_type: str = ""
) -> str | None:
    """Check a payload prefix for error pages and common named file formats."""
    if not prefix:
        return "not a file (empty response)"
    extension = extension.lower()
    html = prefix.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    if extension not in {".html", ".htm", ".xhtml"} and (
        html.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
        or (
            content_type in {"text/html", "application/xhtml+xml"}
            and html.startswith(b"<")
        )
    ):
        return "not a file (HTML page)"
    signature = _FILE_SIGNATURES.get(extension)
    if signature is not None and not prefix.startswith(signature):
        return f"returned content is not a {extension[1:].upper()} file"
    return None


def file_size_limit_reason(size: int, limit: int, *, minimum: bool = False) -> str:
    qualifier = "at least " if minimum else ""
    return f"too large ({qualifier}{size:,} bytes; limit {limit:,} bytes)"


def paper_size_limit_reason(limit: int) -> str:
    return f"over the per-paper size limit ({limit:,} bytes)"


def _extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""


def _url_filename(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return name if _extension(name) else ""


def _user_agent() -> str:
    from .network import USER_AGENT

    return USER_AGENT
