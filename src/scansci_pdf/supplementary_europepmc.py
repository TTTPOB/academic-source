"""Discover and retrieve supplementary files through Europe PMC."""

from __future__ import annotations

import re
import time
import zlib
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryFile
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile, ZipInfo

from requests import RequestException, Response

from .supplementary import (
    SavedSupplementary,
    SupplementaryItem,
    SupplementaryResult,
    UnsavedSupplementary,
    file_size_limit_reason,
    file_validation_error,
    paper_size_limit_reason,
)
from .upstream_errors import RateLimited

_REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_Fetch = Callable[[str], Response]
_CHUNK_BYTES = 64 * 1024
_ATTEMPTS = 3
_RETRY_DELAY = 2.0


def _retrying(fetch: _Fetch) -> _Fetch:
    """Europe PMC intermittently answers 503 or stalls; a retry usually succeeds."""

    def call(url: str) -> Response:
        for attempt in range(_ATTEMPTS):
            last = attempt == _ATTEMPTS - 1
            try:
                response = fetch(url)
            except RequestException:
                if last:
                    raise
            else:
                if last or response.status_code not in {500, 502, 503, 504}:
                    return response
                response.close()
            time.sleep(_RETRY_DELAY * (attempt + 1))
        raise AssertionError("unreachable")

    return call


def _resolve_pmcid(doi: str, fetch: _Fetch) -> str | None:
    query = urlencode({"query": f'DOI:"{doi}"', "format": "json", "resultType": "lite"})
    try:
        with fetch(f"{_REST}/search?{query}") as response:
            response.raise_for_status()
            results = response.json()["resultList"]["result"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(results, list):
        return None
    for record in results:
        if (
            not isinstance(record, dict)
            or str(record.get("doi", "")).casefold() != doi.casefold()
        ):
            continue
        if record.get("hasSuppl") == "N":
            # Europe PMC holds no supplementary files for this article.
            return None
        pmcid = record.get("pmcid")
        if isinstance(pmcid, str) and re.fullmatch(r"PMC[0-9]+", pmcid):
            return pmcid
    return None


def _plain_text(node: ET.Element | None) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


def _caption_text(caption: ET.Element | None) -> str:
    if caption is None:
        return ""
    paragraphs = []
    inline = caption.text or ""
    for node in caption:
        if node.tag.rsplit("}", 1)[-1] in ("title", "p"):
            paragraphs.extend((inline.strip(), _plain_text(node)))
            inline = ""
        else:
            inline += "".join(node.itertext())
        inline += node.tail or ""
    paragraphs.append(inline.strip())
    return "\n\n".join(filter(None, paragraphs))


def _attachments(article: ET.Element, zip_url: str) -> dict[str, SupplementaryItem]:
    files: dict[str, SupplementaryItem] = {}
    for block in article.iterfind(".//{*}supplementary-material"):
        caption = block.find("{*}caption")
        title = caption.find("{*}title") if caption is not None else None
        label = _plain_text(block.find("{*}label")) or _plain_text(title)
        description = _caption_text(caption)
        for node in (block, *block.iterfind(".//{*}media")):
            href = node.get(_XLINK_HREF, "").strip()
            if not href or href.startswith("#") or "://" in href:
                continue
            files.setdefault(href, SupplementaryItem(zip_url, label, description, href))
    return files


def _safe_member_path(root: Path, name: str) -> Path | None:
    windows = PureWindowsPath(name)
    relative = PurePosixPath(name)
    if (
        not relative.parts
        or relative.is_absolute()
        or windows.drive
        or "\\" in name
        or ".." in relative.parts
    ):
        return None
    path = root / relative
    try:
        if path.resolve().is_relative_to(root):
            return path
    except (OSError, RuntimeError):
        pass
    return None


def _extract_archive(
    archive: ZipFile,
    out: Path,
    items: dict[str, SupplementaryItem],
    zip_url: str,
    source_page: str,
    max_file_bytes: int,
    max_total_bytes: int,
    result: SupplementaryResult,
) -> None:
    root = out.resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths: list[tuple[ZipInfo, Path]] = []
    reported: set[str] = set()
    for member in archive.infolist():
        if member.is_dir():
            continue
        path = _safe_member_path(root, member.filename)
        if path is None:
            if member.filename in items:
                reported.add(member.filename)
            item = items.get(member.filename) or SupplementaryItem(
                zip_url, filename=member.filename
            )
            result.unsaved.append(
                UnsavedSupplementary(item, "unsafe Europe PMC ZIP member path")
            )
        else:
            paths.append((member, path))

    order = {name: index for index, name in enumerate(items)}
    exact_members = {member.filename for member, _ in paths} & items.keys()
    by_basename: dict[str, list[str]] = defaultdict(list)
    for name in items.keys() - exact_members:
        by_basename[PurePosixPath(name).name.casefold()].append(name)
    entries: list[tuple[int, ZipInfo, Path, SupplementaryItem]] = []
    for member, path in paths:
        name, basename = member.filename, path.name
        candidates = by_basename.get(basename.casefold(), [])
        match = (
            name if name in items else candidates[0] if len(candidates) == 1 else None
        )
        if match is not None:
            reported.add(match)
        item = (
            items[match]
            if match is not None
            else SupplementaryItem(zip_url, basename, filename=basename)
        )
        entries.append(
            (
                order.get(match, len(order)) if match is not None else len(order),
                member,
                path,
                item,
            )
        )

    written = 0
    total_exhausted = False
    total_reason = paper_size_limit_reason(max_total_bytes)
    for _, member, path, item in sorted(entries, key=lambda entry: entry[0]):
        if total_exhausted or written == max_total_bytes:
            result.unsaved.append(UnsavedSupplementary(item, total_reason))
            continue
        if member.file_size > max_file_bytes:
            result.unsaved.append(
                UnsavedSupplementary(
                    item, file_size_limit_reason(member.file_size, max_file_bytes)
                )
            )
            continue
        budget = max_total_bytes - written
        if member.file_size > budget:
            total_exhausted = True
            result.unsaved.append(UnsavedSupplementary(item, total_reason))
            continue
        # Keep the stream bounded too; a declared size is not permission to write.
        limit = min(max_file_bytes, budget)
        size = 0
        created = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src:
                prefix = src.read(min(1024, limit + 1))
                if len(prefix) <= limit and (
                    error := file_validation_error(prefix, path.suffix.lower())
                ):
                    result.unsaved.append(UnsavedSupplementary(item, error))
                    continue
                with path.open("xb") as dest:
                    created = True
                    chunk = prefix
                    while chunk:
                        size += len(chunk)
                        if size > limit:
                            raise _SizeLimitError
                        dest.write(chunk)
                        chunk = src.read(min(_CHUNK_BYTES, limit - size + 1))
        except _SizeLimitError:
            if created:
                path.unlink()
            if budget < max_file_bytes:
                total_exhausted = True
                reason = total_reason
            else:
                reason = file_size_limit_reason(size, max_file_bytes, minimum=True)
            result.unsaved.append(UnsavedSupplementary(item, reason))
        except (BadZipFile, EOFError, OSError, RuntimeError, zlib.error):
            if created:
                path.unlink()
            result.unsaved.append(
                UnsavedSupplementary(item, "could not extract Europe PMC ZIP member")
            )
        else:
            written += size
            result.saved.append(SavedSupplementary(path, item, source_page))

    for name, item in items.items():
        if name not in reported:
            result.unsaved.append(
                UnsavedSupplementary(item, "file is absent from the Europe PMC ZIP")
            )


class _SizeLimitError(Exception):
    pass


def fetch_europepmc_supplementary(
    doi: str,
    output_dir: Path,
    fetch: _Fetch,
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> SupplementaryResult:
    """Read JATS metadata and stream its supplementary bundle within the caller's limits."""
    result = SupplementaryResult(source="Europe PMC", route="Europe PMC")
    fetch = _retrying(fetch)
    try:
        pmcid = _resolve_pmcid(doi.strip(), fetch)
    except (RequestException, RateLimited):
        result.route_warnings.append("Europe PMC DOI lookup failed")
        return result
    if pmcid is None:
        return result
    result.pmcid = pmcid

    xml_url = f"{_REST}/{pmcid}/fullTextXML"
    try:
        with fetch(xml_url) as response:
            response.raise_for_status()
            source_page = response.url or xml_url
            article = ET.fromstring(response.content)
    except (RequestException, RateLimited, ET.ParseError):
        result.pages_unread = 1
        result.route_warnings.append("Europe PMC full-text XML unavailable")
        return result
    if article.tag.rsplit("}", 1)[-1] != "article":
        result.pages_unread = 1
        result.route_warnings.append(
            "Europe PMC full-text XML did not contain an article"
        )
        return result
    result.pages_read = 1

    zip_url = f"{_REST}/{pmcid}/supplementaryFiles"
    items = _attachments(article, zip_url)
    if not items:
        return result
    try:
        with TemporaryFile() as bundle:
            with fetch(zip_url) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length", "")
                if length.isdecimal() and int(length) > max_total_bytes:
                    raise _SizeLimitError
                received = 0
                for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                    received += len(chunk)
                    if received > max_total_bytes:
                        raise _SizeLimitError
                    bundle.write(chunk)
            bundle.seek(0)
            with ZipFile(bundle) as archive:
                _extract_archive(
                    archive,
                    output_dir,
                    items,
                    zip_url,
                    source_page,
                    max_file_bytes,
                    max_total_bytes,
                    result,
                )
    except _SizeLimitError:
        failure = "Europe PMC ZIP exceeds compressed size limit"
    except (BadZipFile, OSError, RequestException, RateLimited):
        failure = "could not fetch or read the Europe PMC ZIP"
    else:
        if not result.saved:
            result.route_warnings.append("Europe PMC ZIP yielded no files")
        return result
    result.route_warnings.append(failure)
    result.unsaved.extend(
        UnsavedSupplementary(item, failure) for item in items.values()
    )
    return result
