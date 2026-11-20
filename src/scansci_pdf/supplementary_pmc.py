"""Discover supplementary files in NCBI's public PMC Cloud bucket."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree as ET

from requests import RequestException
from requests.exceptions import ChunkedEncodingError

from .pmc_cloud import CloudFile, Fetch, find_article, lookup_pmcid
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

_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_CHUNK_BYTES = 64 * 1024


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


def _attachments(article: ET.Element, source_page: str) -> dict[str, SupplementaryItem]:
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
            files.setdefault(
                href, SupplementaryItem(source_page, label, description, href)
            )
    return files


_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".gif", ".png", ".tif", ".tiff"}


def _image_key(name: str) -> str:
    path = PurePosixPath(name.casefold())
    return path.stem if path.suffix in _IMAGE_SUFFIXES else path.name


def _article_images(article: ET.Element) -> set[str]:
    """Files the article body shows as figures or formulas, in any rendition."""
    supplementary = {
        node.get(_XLINK_HREF, "")
        for block in article.iterfind(".//{*}supplementary-material")
        for node in block.iter()
    }
    return {
        _image_key(href)
        for tag in ("graphic", "inline-graphic")
        for node in article.iterfind(f".//{{*}}{tag}")
        if (href := node.get(_XLINK_HREF, "").strip()) and href not in supplementary
    }


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


def _attachment_name(file: CloudFile) -> str:
    return file.key.partition("/")[2]


def _match_attachments(
    files: tuple[CloudFile, ...], article: ET.Element, source_page: str
) -> Iterator[tuple[CloudFile | None, SupplementaryItem]]:
    """Prefer exact paths, then match remaining unique basenames in JATS order."""
    items = _attachments(article, source_page)
    article_images = _article_images(article)
    candidates: dict[str, list[str]] = defaultdict(list)
    exact: dict[str, CloudFile] = {}
    unmatched: dict[str, CloudFile] = {}
    for file in files:
        name = _attachment_name(file)
        if name.endswith("/"):
            continue
        if name in items:
            exact[name] = file
        elif _image_key(file.name) not in article_images:
            unmatched[name] = file
            candidates[file.name.casefold()].append(name)

    href_counts = Counter(
        PurePosixPath(href).name.casefold() for href in items if href not in exact
    )
    for href, item in items.items():
        file = exact.get(href)
        if file is None:
            basename = PurePosixPath(href).name.casefold()
            matches = candidates.get(basename, [])
            if href_counts[basename] == len(matches) == 1:
                file = unmatched.pop(matches[0])
        yield file, replace(item, url=file.url) if file is not None else item

    for file in unmatched.values():
        yield file, SupplementaryItem(file.url, file.name, filename=file.name)


def _download(
    fetch: Fetch,
    file: CloudFile,
    item: SupplementaryItem,
    path: Path,
    source_page: str,
) -> SavedSupplementary | UnsavedSupplementary:
    created = saved = False
    try:
        with fetch(file.url) as response:
            if response.status_code != 200:
                return UnsavedSupplementary(item, f"HTTP {response.status_code}")
            content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x+b") as dest:
                created = True
                size = 0
                for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                    size += len(chunk)
                    # The listing size is this worker's share of the paper budget.
                    if size > file.size:
                        return UnsavedSupplementary(item, "exceeded PMC listing size")
                    dest.write(chunk)
                if size < file.size:
                    return UnsavedSupplementary(item, "truncated transfer")
                dest.seek(0)
                if reason := file_validation_error(
                    dest.read(1024), path.suffix, content_type=content_type
                ):
                    return UnsavedSupplementary(item, reason)
        saved = True
        return SavedSupplementary(path, item, source_page)
    except RateLimited as exc:
        return UnsavedSupplementary(item, f"HTTP {exc.status_code}")
    except ChunkedEncodingError:
        return UnsavedSupplementary(item, "truncated transfer")
    except RequestException:
        return UnsavedSupplementary(item, "network error")
    except OSError:
        return UnsavedSupplementary(item, "could not write file")
    finally:
        if created and not saved:
            path.unlink(missing_ok=True)


def _save_attachments(
    files: tuple[CloudFile, ...],
    article: ET.Element,
    source_page: str,
    output_dir: Path,
    fetch: Fetch,
    max_file_bytes: int,
    max_total_bytes: int,
    result: SupplementaryResult,
) -> None:
    root = output_dir.resolve()
    remaining = max_total_bytes
    outcomes: list[
        Future[SavedSupplementary | UnsavedSupplementary] | UnsavedSupplementary
    ] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for file, item in _match_attachments(files, article, source_page):
            if file is None:
                reason = "file is absent from PMC Cloud"
            elif (path := _safe_member_path(root, _attachment_name(file))) is None:
                reason = "unsafe PMC attachment path"
            elif file.size > max_file_bytes:
                reason = file_size_limit_reason(file.size, max_file_bytes)
            elif not remaining or file.size > remaining:
                reason = paper_size_limit_reason(max_total_bytes)
                remaining = 0
            else:
                remaining -= file.size
                outcomes.append(
                    pool.submit(_download, fetch, file, item, path, source_page)
                )
                continue
            outcomes.append(UnsavedSupplementary(item, reason))

        # Collect in JATS order, even when a later download finishes earlier.
        for outcome in outcomes:
            entry = outcome.result() if isinstance(outcome, Future) else outcome
            if isinstance(entry, SavedSupplementary):
                result.saved.append(entry)
            else:
                result.unsaved.append(entry)


def fetch_pmc_supplementary(
    doi: str,
    output_dir: Path,
    fetch: Fetch,
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> SupplementaryResult:
    """Read the article's JATS and stream individual PMC Cloud attachments."""
    result = SupplementaryResult(source="PMC", route="PMC")
    try:
        pmcid = lookup_pmcid(doi, fetch)
    except (RequestException, RateLimited):
        result.route_warnings.append("PMC DOI lookup failed")
        return result
    if pmcid is None:
        return result
    result.pmcid = pmcid

    try:
        cloud_article = find_article(pmcid, fetch)
    except (RequestException, RateLimited, ET.ParseError):
        result.route_warnings.append("PMC Cloud file listing unavailable")
        return result
    if cloud_article is None:
        return result
    files = cloud_article.attachments
    if not files:
        return result

    xml = cloud_article.xml
    article = None
    if xml is not None:
        try:
            with fetch(xml.url) as response:
                response.raise_for_status()
                article = ET.fromstring(response.content)
        except (RequestException, RateLimited, ET.ParseError):
            pass
    if xml is None or article is None or article.tag.rsplit("}", 1)[-1] != "article":
        result.pages_unread = 1
        result.route_warnings.append("PMC full-text XML unavailable")
        return result
    result.pages_read = 1
    _save_attachments(
        files,
        article,
        f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
        output_dir,
        fetch,
        max_file_bytes,
        max_total_bytes,
        result,
    )
    return result
