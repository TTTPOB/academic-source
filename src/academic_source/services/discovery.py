"""Normalize paper references and expose safe discovery results."""

import json
import re
from html import unescape
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import requests

from scansci_pdf.identifiers import (
    ARXIV_RE,
    OLD_ARXIV_RE,
    normalize_arxiv_id,
    normalize_doi,
)


def normalize_identifier(value: str) -> str:
    """Keep titles intact while canonicalizing DOI and arXiv references."""
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.hostname and parsed.hostname.lower() in {"arxiv.org", "www.arxiv.org"}:
        path = parsed.path.lstrip("/")
        if path.startswith(("abs/", "pdf/")):
            raw = path.split("/", 1)[1].removesuffix(".pdf")
    arxiv = normalize_arxiv_id(raw)
    if arxiv:
        version = re.search(r"v\d+$", raw, flags=re.IGNORECASE)
        return f"arxiv:{arxiv}{version.group().lower() if version else ''}"
    return normalize_doi(raw)


def get_metadata(
    id_type: Literal["doi", "arxiv"],
    config: dict[str, Any],
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
) -> dict[str, Any]:
    """Fetch bibliographic metadata from the explicitly selected identifier source."""
    if id_type == "doi":
        if doi is None or arxiv_id is not None:
            raise ValueError("DOI metadata requires doi only")
        return _doi_metadata(doi, config)
    if id_type == "arxiv":
        if arxiv_id is None or doi is not None:
            raise ValueError("arXiv metadata requires arxiv_id only")
        return _arxiv_metadata(_metadata_arxiv_id(arxiv_id), config)
    raise ValueError("Metadata id_type must be doi or arxiv")


def _doi_metadata(doi: str, config: dict[str, Any]) -> dict[str, Any]:
    doi = normalize_doi(doi)
    if not re.fullmatch(r"10\.\d+/\S+", doi):
        raise ValueError("Metadata requires a DOI")
    result = {
        "id_type": "doi",
        "doi": doi,
        "metadata_source": "crossref",
    }
    from scansci_pdf.network import _get_session, request_timeout

    session = _get_session(config)
    params = {"mailto": config["email"]} if config.get("email") else {}
    try:
        with session.get(
            f"https://api.crossref.org/works/{quote(doi, safe='')}",
            params=params,
            headers={"Accept": "application/json"},
            timeout=request_timeout(config),
        ) as response:
            if response.status_code == 404:
                return {**result, "metadata_status": "not_found"}
            response.raise_for_status()
            payload = response.json()
    except json.JSONDecodeError:
        return {
            **result,
            "metadata_status": "unavailable",
            "metadata_error": "Crossref returned invalid JSON",
        }
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        return {
            **result,
            "metadata_status": "unavailable",
            "metadata_error": f"Crossref metadata request failed (HTTP {status})",
        }
    except requests.RequestException:
        return {
            **result,
            "metadata_status": "unavailable",
            "metadata_error": "Crossref metadata request failed (network error)",
        }
    message = payload.get("message") if isinstance(payload, dict) else None
    record_doi = message.get("DOI") if isinstance(message, dict) else None
    if (
        not isinstance(record_doi, str)
        or normalize_doi(record_doi).casefold() != doi.casefold()
    ):
        return {
            **result,
            "metadata_status": "unavailable",
            "metadata_error": "Crossref returned an invalid or mismatched DOI record",
        }
    return {
        **result,
        "metadata_status": "found",
        **_crossref_fields(message),
    }


def _metadata_arxiv_id(value: str) -> str:
    """Normalize an entire arXiv reference while preserving its requested version."""
    raw = re.sub(r"^arxiv:\s*", "", normalize_doi(value), flags=re.IGNORECASE)
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.hostname not in {
            "arxiv.org",
            "www.arxiv.org",
        } or not parsed.path.startswith(("/abs/", "/pdf/")):
            raise ValueError("Metadata requires an arXiv ID")
        raw = parsed.path.split("/", 2)[2].removesuffix(".pdf")
    raw = re.sub(r"^10\.48550/arxiv\.", "", raw, flags=re.IGNORECASE)
    if not (ARXIV_RE.fullmatch(raw) or OLD_ARXIV_RE.fullmatch(raw)):
        raise ValueError("Metadata requires an arXiv ID")
    base = normalize_arxiv_id(raw)
    version = re.search(r"v(\d+)$", raw, flags=re.IGNORECASE)
    if version and int(version[1]) < 1:
        raise ValueError("Metadata requires an arXiv ID with a positive version")
    return f"{base}{version.group().lower() if version else ''}"


def _arxiv_metadata(arxiv_id: str, config: dict[str, Any]) -> dict[str, Any]:
    from scansci_pdf.network import _get_session, request_timeout

    result = {
        "id_type": "arxiv",
        "arxiv_id": arxiv_id,
        "metadata_source": "arxiv",
    }
    session = _get_session(config)
    try:
        with session.get(
            "https://export.arxiv.org/api/query",
            params={"id_list": arxiv_id},
            headers={"Accept": "application/atom+xml"},
            timeout=request_timeout(config),
        ) as response:
            response.raise_for_status()
            feed = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        return {
            **result,
            "metadata_status": "unavailable",
            "metadata_error": "arXiv returned invalid XML",
        }
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        return {
            **result,
            "metadata_status": "unavailable",
            "metadata_error": f"arXiv metadata request failed (HTTP {status})",
        }
    except requests.RequestException:
        return {
            **result,
            "metadata_status": "unavailable",
            "metadata_error": "arXiv metadata request failed (network error)",
        }
    atom = "{http://www.w3.org/2005/Atom}"
    invalid = {
        **result,
        "metadata_status": "unavailable",
        "metadata_error": "arXiv returned an invalid or mismatched record",
    }
    if feed.tag != f"{atom}feed":
        return invalid
    entries = feed.findall(f"{atom}entry")
    if not entries:
        return {**result, "metadata_status": "not_found"}
    if len(entries) != 1:
        return invalid
    entry = entries[0]
    record_url = entry.findtext(f"{atom}id", "").strip()
    try:
        parsed = urlsplit(record_url)
        if parsed.hostname not in {
            "arxiv.org",
            "www.arxiv.org",
        } or not parsed.path.startswith("/abs/"):
            return invalid
        record_id = _metadata_arxiv_id(record_url)
    except ValueError:
        return invalid
    version = re.search(r"v(\d+)$", record_id)
    requested_version = re.search(r"v\d+$", arxiv_id)
    if (
        re.sub(r"v\d+$", "", record_id).casefold()
        != re.sub(r"v\d+$", "", arxiv_id).casefold()
    ):
        return invalid
    if requested_version and record_id.casefold() != arxiv_id.casefold():
        return invalid

    def text(tag: str) -> str:
        return " ".join(entry.findtext(tag, "").split())

    published = text(f"{atom}published")
    fields = {
        "title": text(f"{atom}title"),
        "authors": [
            name
            for author in entry.findall(f"{atom}author")
            if (name := " ".join(author.findtext(f"{atom}name", "").split()))
        ],
        "year": int(published[:4]) if re.match(r"^\d{4}-", published) else None,
        "abstract": text(f"{atom}summary"),
        "published": published,
        "updated": text(f"{atom}updated"),
        "version": int(version[1]) if version else None,
        "published_doi": normalize_doi(text("{http://arxiv.org/schemas/atom}doi")),
    }
    return {
        **result,
        "metadata_status": "found",
        **{key: value for key, value in fields.items() if value not in (None, "", [])},
    }


def _crossref_fields(message: dict[str, Any]) -> dict[str, Any]:
    """Project bibliographic and directed relationship evidence, never the raw record."""
    titles = message.get("title") or []
    title = unescape(re.sub(r"<[^>]+>", "", titles[0])).strip() if titles else ""
    authors = []
    for author in message.get("author") or []:
        name = " ".join(filter(None, [author.get("given"), author.get("family")]))
        name = name or author.get("name")
        if name:
            authors.append(name)
    year = None
    for field in ("published", "published-online", "published-print", "issued"):
        parts = (message.get(field) or {}).get("date-parts") or []
        if parts and parts[0] and isinstance(parts[0][0], int):
            year = parts[0][0]
            break
    relations = [
        {"type": kind, "doi": normalize_doi(relation["id"])}
        for kind, entries in (message.get("relation") or {}).items()
        for relation in entries
        if relation.get("id-type", "doi").lower() == "doi" and relation.get("id")
    ]
    updates = [
        {
            "direction": direction,
            "type": update.get("type"),
            "doi": normalize_doi(update["DOI"]),
        }
        for direction in ("update-to", "updated-by")
        for update in message.get(direction) or []
        if update.get("DOI") and update.get("type")
    ]
    fields = {
        "title": title,
        "authors": authors,
        "year": year,
        "type": message.get("type"),
        "relations": relations,
        "review_type": (message.get("review") or {}).get("type"),
        "updates": updates,
    }
    return {
        key: value for key, value in fields.items() if value not in (None, "", [], {})
    }


def search(
    query: str, limit: int = 10, *, config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    if not query.strip():
        raise ValueError("Search query must not be empty")
    if not 1 <= limit <= 100:
        raise ValueError("Search limit must be between 1 and 100")
    from scansci_pdf.search import search_papers

    fields = (
        "doi",
        "title",
        "authors",
        "year",
        "cited_by_count",
        "is_oa",
        "oa_url",
        "abstract",
        "url",
        "source",
        "type",
        "is_retracted",
        "publication_date",
    )
    return [
        {
            key: item[key]
            for key in fields
            if key in item and item[key] not in (None, "", [], {})
        }
        for item in search_papers(query.strip(), limit=limit, config=config)
    ]
