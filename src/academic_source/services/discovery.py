"""Normalize paper references and expose safe discovery results."""

import re
from typing import Any
from urllib.parse import urlsplit

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
    if normalized.startswith(("10.", "arxiv:")):
        return {
            "identifier": normalized,
            "doi": normalized if normalized.startswith("10.") else None,
        }
    from scansci_pdf.resolver import resolve_title_to_doi

    doi = resolve_title_to_doi(normalized, config)
    return {
        "identifier": normalize_identifier(doi) if doi else normalized,
        "doi": doi,
        "title": normalized,
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
    )
    return [
        {key: item[key] for key in fields if key in item}
        for item in search_papers(query.strip(), limit=limit)
    ]
