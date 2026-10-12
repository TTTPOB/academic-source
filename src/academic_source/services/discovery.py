"""Normalize paper references and expose safe discovery results."""

import json
import re
from html import unescape
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from scansci_pdf.identifiers import normalize_arxiv_id, normalize_doi


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


def resolve(identifier: str, config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_identifier(identifier)
    if not normalized:
        raise ValueError("Identifier must not be empty")
    if normalized.startswith("10."):
        return {
            "identifier": normalized,
            "doi": normalized,
            **_crossref_metadata(normalized, config),
        }
    if normalized.startswith("arxiv:"):
        return {"identifier": normalized, "doi": None}
    from scansci_pdf.resolver import resolve_title_to_doi

    doi = resolve_title_to_doi(normalized, config)
    return {
        "identifier": normalize_identifier(doi) if doi else normalized,
        "doi": doi,
        "title": normalized,
    }


def _crossref_metadata(doi: str, config: dict[str, Any]) -> dict[str, Any]:
    """Enrich an explicitly supplied DOI without confusing lookup and normalization."""
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
                return {"metadata_status": "not_found"}
            response.raise_for_status()
            payload = response.json()
    except json.JSONDecodeError:
        return {
            "metadata_status": "unavailable",
            "metadata_error": "Crossref returned invalid JSON",
        }
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        return {
            "metadata_status": "unavailable",
            "metadata_error": f"Crossref metadata request failed (HTTP {status})",
        }
    except requests.RequestException:
        return {
            "metadata_status": "unavailable",
            "metadata_error": "Crossref metadata request failed (network error)",
        }
    message = payload.get("message") if isinstance(payload, dict) else None
    if (
        not isinstance(message, dict)
        or normalize_doi(message.get("DOI") or "").casefold() != doi.casefold()
    ):
        return {
            "metadata_status": "unavailable",
            "metadata_error": "Crossref returned an invalid or mismatched DOI record",
        }
    return {
        "metadata_status": "found",
        "metadata_source": "crossref",
        **_crossref_fields(message),
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


def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
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
        for item in search_papers(query.strip(), limit=limit)
    ]
