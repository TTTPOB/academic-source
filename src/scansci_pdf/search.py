"""Paper search and title matching through OpenAlex."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Literal

import requests

from .identifiers import normalize_doi
from .network import _get_session
from .upstream_client import openalex_params, request_response
from .upstream_errors import UpstreamError

_MAX_LIMIT = 1000
_PAGE_SIZE = 100  # OpenAlex's per-page maximum; larger limits page with a cursor.
_OPENALEX_NON_PAPER_TYPES = {
    "book",
    "book-chapter",
    "conference-abstract",
    "dataset",
    "erratum",
    "paratext",
    "peer-review",
    "reference-entry",
    "retraction",
    "standard",
    "supplementary-materials",
}
# Legacy/raw labels are not accepted by the OpenAlex work type filter.
_NON_PAPER_TYPES = _OPENALEX_NON_PAPER_TYPES | {
    "book-part",
    "book-section",
    "book-series",
    "book-set",
    "component",
    "correction",
    "edited-book",
    "grant",
    "monograph",
    "proceedings",
    "reference-book",
}
SearchScope = Literal["all", "title", "abstract"]
_SEARCH_KEYS = {
    "all": "search",
    "title": "search.title",
    "abstract": "search.title_and_abstract",
}
_ISSN = re.compile(r"[0-9]{4}-[0-9]{3}[0-9X]", re.IGNORECASE)
_AUTHOR_ID = re.compile(r"(?:https?://openalex\.org/)?(A[0-9]+)", re.IGNORECASE)
_ORCID = re.compile(
    r"(?:https?://orcid\.org/)?([0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X])",
    re.IGNORECASE,
)
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
) -> dict[str, Any]:
    config = config if config is not None else {}
    raw_fields = _filter_fields(params.get("filter", ""))
    is_search = any(key in params for key in _SEARCH_KEYS.values()) or any(
        field.endswith((".search", ".search.exact")) for field in raw_fields
    )
    try:
        response = request_response(
            f"https://api.openalex.org/{entity}",
            config,
            session=_get_session(config),
            source="openalex",
            operation="search" if is_search else "list",
            params=openalex_params(config, params),
        )
    except requests.RequestException:
        raise UpstreamError(
            "openalex", "OpenAlex: request failed (network error)"
        ) from None
    with response:
        status = response.status_code
        if status >= 400:
            message = f"OpenAlex: request failed (HTTP {status})"
            if status == 400:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and isinstance(body.get("message"), str):
                    message = body["message"] or message
            raise UpstreamError("openalex", message, status_code=status)
        try:
            data = response.json()
        except ValueError:
            raise UpstreamError("openalex", "OpenAlex: returned invalid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise UpstreamError("openalex", "OpenAlex returned an invalid results payload")
    return data


def _filter_fields(expression: str) -> set[str]:
    # Inspect field names only. The full expression is passed through untouched.
    return {
        name.strip().lower().replace("-", "_")
        for clause in expression.split(",")
        for name, colon, _ in [clause.partition(":")]
        if colon
    }


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
    return " ".join(word for _, word in word_positions)


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
        source = (work.get("primary_location") or {}).get("source") or {}
        journal = source.get("display_name")
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
                **({"journal": journal} if journal else {}),
            }
        )
    return results


def _paper_results(
    works: list[dict], limit: int, *, exclude_non_papers: bool, exclude_retracted: bool
) -> list[dict[str, Any]]:
    results = []
    seen = set()
    for item in _parse_openalex_works(works):
        if exclude_retracted and item["is_retracted"] is True:
            continue
        if exclude_non_papers and (
            item["type"] in _NON_PAPER_TYPES
            or _normalized_title(item["title"]) in _PARATEXT_TITLES
        ):
            continue
        doi_key = item["doi"].casefold()
        if doi_key in seen:
            continue
        seen.add(doi_key)
        results.append(item)
        if len(results) == limit:
            break
    return results


def _year_bounds(year: str) -> tuple[int | None, int | None]:
    if re.fullmatch(r"[0-9]{4}", year):
        return int(year), int(year)
    match = re.fullmatch(r"([0-9]{4})?-([0-9]{4})?", year)
    if not match or not any(match.groups()):
        raise ValueError("Year must be YYYY, YYYY-YYYY, YYYY-, or -YYYY")
    return int(match[1]) if match[1] else None, int(match[2]) if match[2] else None


def _sort_param(sort: str) -> str:
    key, _, direction = sort.partition(":")
    direction = direction or "desc"
    if key not in {"cited_by_count", "publication_date"} or direction not in {
        "asc",
        "desc",
    }:
        raise ValueError(
            "Sort must be cited_by_count or publication_date, optionally :asc or :desc"
        )
    return f"{key}:{direction}"


def _journal_filter(journal: str, config: dict[str, Any] | None) -> str:
    if _ISSN.fullmatch(journal):
        return f"primary_location.source.issn:{journal.upper()}"

    name = journal.strip().casefold()
    candidates: list[dict[str, Any]] = []
    matches: dict[str, dict[str, Any]] = {}
    named: dict[str, dict[str, Any]] = {}
    # One relevance-ranked page: an exact title match ranks near the top, and
    # paging through every partial match (e.g. 300 for "Nature") costs a search each.
    page = _openalex_get(
        "sources",
        {
            "search": journal,
            "per_page": 100,
            "select": "id,display_name,alternate_titles,abbreviated_title,issn_l",
        },
        config,
    )
    for source in page["results"]:
        if len(candidates) < 10:
            candidates.append(source)
        titles = (
            source.get("display_name"),
            source.get("abbreviated_title"),
            *(source.get("alternate_titles") or []),
        )
        if any(title and title.strip().casefold() == name for title in titles):
            matches[source["id"]] = source
            if (source.get("display_name") or "").strip().casefold() == name:
                named[source["id"]] = source
    # A journal's own name beats another journal's alternate title:
    # "Cell" is Cell, even though Developmental Cell lists "Cell" as an alias.
    for chosen in (named, matches):
        if len(chosen) == 1:
            source_id = next(iter(chosen)).rsplit("/", 1)[-1]
            return f"primary_location.source.id:{source_id}"
    suggestions = list(matches.values()) or candidates
    listed = "; ".join(
        f"{source.get('display_name') or '(unnamed)'} "
        f"(ISSN-L: {source.get('issn_l') or 'unavailable'})"
        for source in suggestions[:10]
    )
    problem = "is ambiguous" if matches else "has no exact source match"
    raise ValueError(
        f"Journal {journal!r} {problem}. Retry with an ISSN. "
        f"Candidates: {listed or 'none'}"
    )


def _lookup_author_id(
    name: str,
    config: dict[str, Any] | None = None,
) -> tuple[str | None, str, int, int]:
    candidates = _openalex_get("authors", {"search": name, "per_page": 10}, config)[
        "results"
    ]
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
    journal: str | None = None,
    year: str | None = None,
    type: str | None = None,
    oa: bool = False,
    min_citations: int | None = None,
    in_: SearchScope = "all",
    filter: str | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Search one bounded OpenAlex works page, then deduplicate by DOI."""
    if not 1 <= limit <= _MAX_LIMIT:
        raise ValueError(f"Search limit must be between 1 and {_MAX_LIMIT}")
    if in_ not in _SEARCH_KEYS:
        raise ValueError("Search scope must be all, title, or abstract")

    query = query.strip()
    journal = (journal or "").strip()
    year = (year or "").strip()
    types = [value.strip() for value in (type or "").split(",") if value.strip()]
    author = (author or "").strip()
    raw_filter = filter if filter and filter.strip() else ""
    params: dict[str, Any] = {"per_page": min(_PAGE_SIZE, max(30, limit * 3))}
    if query:
        params[_SEARCH_KEYS[in_]] = query
    if sort or not query:
        # Without keywords, newest first is useful for browsing a filtered list.
        params["sort"] = _sort_param(sort) if sort else "publication_date:desc"

    filters = []
    if year:
        if year_from is not None or year_to is not None:
            raise ValueError("Year cannot be combined with year_from or year_to")
        year_from, year_to = _year_bounds(year)
    if any(
        bound is not None and not 1 <= bound <= 9999 for bound in (year_from, year_to)
    ):
        raise ValueError("Year must be between 0001 and 9999")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ValueError("The starting year must not exceed the ending year")
    if year_from is not None:
        filters.append(f"from_publication_date:{year_from:04d}-01-01")
    if year_to is not None:
        filters.append(f"to_publication_date:{year_to:04d}-12-31")
    if types:
        if any(
            not re.fullmatch(r"[a-z0-9-]+", value, re.IGNORECASE) for value in types
        ):
            raise ValueError(
                "Type must be a comma-separated list of OpenAlex work types"
            )
        filters.append("type:" + "|".join(types))
    if oa:
        filters.append("open_access.is_oa:true")
    if min_citations is not None:
        if min_citations < 0:
            raise ValueError("Minimum citations must be non-negative")
        filters.append(f"cited_by_count:>{min_citations - 1}")
    if not (query or filters or journal or author or author_id or raw_filter):
        raise ValueError("Search requires a query or at least one filter")

    author_match = None
    if author_id:
        filters.append(f"authorships.author.id:{author_id}")
    elif author:
        if match := _AUTHOR_ID.fullmatch(author):
            filters.append(f"authorships.author.id:{match[1].upper()}")
        elif match := _ORCID.fullmatch(author):
            filters.append(f"author.orcid:{match[1].upper()}")
        else:
            author_id, name, works, cited = _lookup_author_id(author, config)
            if not author_id:
                return []
            filters.append(f"authorships.author.id:{author_id}")
            author_match = {
                "name": name,
                "id": author_id,
                "works_count": works,
                "cited_by_count": cited,
            }
    if journal:
        filters.append(_journal_filter(journal, config))

    raw_fields = _filter_fields(raw_filter)
    exclude_non_papers = not types and "type" not in raw_fields
    exclude_retracted = "is_retracted" not in raw_fields
    if exclude_non_papers:
        # OpenAlex combines repeated negations with AND, not the + syntax.
        filters.extend(f"type:!{kind}" for kind in sorted(_OPENALEX_NON_PAPER_TYPES))
    if exclude_retracted:
        filters.append("is_retracted:false")
    if raw_filter:
        filters.append(raw_filter)
    params["filter"] = ",".join(filters)
    works = _search_pages(params, limit, config)
    results = _paper_results(
        works,
        limit,
        exclude_non_papers=exclude_non_papers,
        exclude_retracted=exclude_retracted,
    )
    if results and author_match:
        results[0]["_author_match"] = author_match
    return results


def _search_pages(
    params: dict[str, Any], limit: int, config: dict[str, Any] | None
) -> list[dict]:
    """One request up to a page; beyond that, follow OpenAlex's cursor."""
    if limit <= _PAGE_SIZE:
        return _openalex_get("works", params, config)["results"]
    works: list[dict] = []
    cursor = "*"
    # A little slack for the few records dropped locally (paratext, duplicates).
    while cursor and len(works) < limit + _PAGE_SIZE // 10:
        page = _openalex_get("works", {**params, "cursor": cursor}, config)
        works.extend(page["results"])
        cursor = (page.get("meta") or {}).get("next_cursor") if page["results"] else None
    return works


def search_by_title(
    title: str, config: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return a sufficiently similar, unambiguous OpenAlex title match with a DOI."""
    if not title or len(title.strip()) < 10:
        return None
    works = _openalex_get("works", {"search": title, "per_page": 5}, config)["results"]
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
