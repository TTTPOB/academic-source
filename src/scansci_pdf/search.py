"""Paper search and title matching through OpenAlex."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher
from typing import Any

from .identifiers import normalize_doi
from .upstream_client import get_json, openalex_params
from .upstream_errors import UpstreamError

_MAX_LIMIT = 100
_MAX_CANDIDATES = 100  # OpenAlex's per-page maximum; never paginate to fill a limit.
_NON_PAPER_TYPES = {
    "peer-review",
    "component",
    "supplementary-materials",
    "paratext",
    "reference-entry",
    "dataset",
    "erratum",
    "correction",
    "retraction",
    "conference-abstract",
    "book",
    "book-chapter",
    "book-section",
    "book-part",
    "book-series",
    "book-set",
    "reference-book",
    "monograph",
    "edited-book",
    "proceedings",
    "standard",
    "grant",
}
_PARATEXT_TITLES = {
    "foreword",
    "manuscript submission guideline",
    "manuscript submission guidelines",
    "instructions for authors",
    "author guidelines",
    "editorial board",
    "organizing committee",
}


def _openalex_get(
    entity: str,
    params: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    data = get_json(
        f"https://api.openalex.org/{entity}",
        config,
        source="openalex",
        operation="search" if "search" in params else "list",
        params=openalex_params(config or {}, params),
    )
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise UpstreamError("openalex", "OpenAlex returned an invalid results payload")
    return data["results"]


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    if not isinstance(inverted_index, dict):
        return ""
    word_positions = [
        (position, word)
        for word, positions in inverted_index.items()
        if isinstance(positions, list)
        for position in positions
        if isinstance(position, int)
    ]
    word_positions.sort()
    return " ".join(word for _, word in word_positions)[:500]


def _normalized_title(title: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", title).casefold()))


def _parse_openalex_works(works: list[dict]) -> list[dict[str, Any]]:
    """Keep source order and useful evidence; missing dates have one representation."""
    results = []
    for work in works:
        doi = normalize_doi(work.get("doi") or "")
        if not doi:
            continue
        raw_date = work.get("publication_date")
        publication_date = None
        if raw_date:
            try:
                publication_date = date.fromisoformat(raw_date).isoformat()
            except ValueError:
                pass
        year = work.get("publication_year")
        if not isinstance(year, int):
            year = int(year) if isinstance(year, str) and year.isdigit() else None
        oa = work.get("open_access") or {}
        best_oa = work.get("best_oa_location") or {}
        results.append(
            {
                "title": work.get("title") or "",
                "doi": doi,
                "url": work.get("id") or "",
                "authors": [
                    entry["author"]["display_name"]
                    for entry in (work.get("authorships") or [])[:5]
                    if (entry.get("author") or {}).get("display_name")
                ],
                "year": year,
                "publication_date": publication_date,
                "cited_by_count": work.get("cited_by_count") or 0,
                "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
                "is_oa": oa.get("is_oa", False),
                "oa_url": best_oa.get("pdf_url")
                or best_oa.get("landing_page_url")
                or oa.get("oa_url")
                or "",
                "source": "openalex",
                "type": work.get("type"),
                "is_retracted": work.get("is_retracted"),
            }
        )
    return results


def _paper_results(works: list[dict], limit: int) -> list[dict[str, Any]]:
    results = []
    seen = set()
    for item in _parse_openalex_works(works):
        if item["type"] in _NON_PAPER_TYPES or item["is_retracted"] is True:
            continue
        if _normalized_title(item["title"]) in _PARATEXT_TITLES:
            continue
        doi_key = item["doi"].casefold()
        if doi_key in seen:
            continue
        seen.add(doi_key)
        results.append(item)
        if len(results) == limit:
            break
    return results


def _work_params(
    limit: int,
    year_from: int | None,
    year_to: int | None,
    sort: str | None,
) -> dict[str, Any]:
    if not 1 <= limit <= _MAX_LIMIT:
        raise ValueError("Search limit must be between 1 and 100")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ValueError("year_from must not exceed year_to")
    params: dict[str, Any] = {"per_page": min(_MAX_CANDIDATES, max(30, limit * 3))}
    filters = []
    if year_from is not None:
        filters.append(f"from_publication_date:{year_from:04d}-01-01")
    if year_to is not None:
        filters.append(f"to_publication_date:{year_to:04d}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    if sort:
        key, _, direction = sort.partition(":")
        direction = direction or "desc"
        if key not in {"cited_by_count", "publication_date"} or direction not in {
            "asc",
            "desc",
        }:
            raise ValueError(
                "Sort must be cited_by_count or publication_date, optionally :asc or :desc"
            )
        params["sort"] = f"{key}:{direction}"
    return params


def _lookup_author_id(
    name: str,
    config: dict[str, Any] | None = None,
) -> tuple[str | None, str, int, int]:
    candidates = _openalex_get("authors", {"search": name, "per_page": 10}, config)
    if not candidates:
        return None, "", 0, 0
    parts = name.casefold().split()
    variants = {" ".join(parts)}
    if len(parts) >= 2:
        variants.add(f"{parts[-1]} {' '.join(parts[:-1])}")
    matches = [
        author
        for author in candidates
        if (author.get("display_name") or "").casefold().strip() in variants
    ]
    best = max(
        matches or candidates,
        key=lambda author: (
            author.get("cited_by_count") or 0,
            author.get("works_count") or 0,
        ),
    )
    author_id = (best.get("id") or "").rsplit("/", 1)[-1]
    return (
        author_id or None,
        best.get("display_name") or name,
        best.get("works_count") or 0,
        best.get("cited_by_count") or 0,
    )


def search_papers(
    query: str = "",
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str | None = None,
    *,
    author: str | None = None,
    author_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Search one bounded OpenAlex page, filter non-papers, then apply limit.

    Default order is OpenAlex relevance; explicit date/citation sort stays upstream.
    Author searches retain the existing name-to-profile matching behavior.
    """
    params = _work_params(limit, year_from, year_to, sort)
    author_match = None
    if author and not author_id:
        author_id, name, works, cited = _lookup_author_id(author, config)
        if not author_id:
            return []
        author_match = {
            "name": name,
            "id": author_id,
            "works_count": works,
            "cited_by_count": cited,
        }
    if author_id:
        author_filter = f"authorships.author.id:{author_id}"
        params["filter"] = ",".join(filter(None, [params.get("filter"), author_filter]))
    elif query.strip():
        params["search"] = query.strip()
    else:
        return []
    results = _paper_results(_openalex_get("works", params, config), limit)
    if results and author_match:
        results[0]["_author_match"] = author_match
    return results


def search_by_title(
    title: str, config: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return a sufficiently similar, unambiguous OpenAlex title match with a DOI."""
    if not title or len(title.strip()) < 10:
        return None
    works = _openalex_get("works", {"search": title, "per_page": 5}, config)
    normalized = _normalized_title(title)
    matches = []
    for item in _parse_openalex_works(works):
        if not item["title"]:
            continue
        score = SequenceMatcher(
            None, normalized, _normalized_title(item["title"])
        ).ratio()
        matches.append((score, item))
    if not matches:
        return None
    matches.sort(key=lambda match: match[0], reverse=True)
    score, best = matches[0]
    if score < 0.75:
        return None
    if any(
        other_score == score and other["doi"].casefold() != best["doi"].casefold()
        for other_score, other in matches[1:]
    ):
        return None
    return {**best, "score": round(score, 3)}
