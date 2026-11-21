"""Search Crossref works using its native query and filter parameters."""

import re
from datetime import date
from html import unescape
from typing import Any

import requests

from .identifiers import normalize_doi
from .network import _get_session
from .upstream_client import request_response
from .upstream_errors import UpstreamError

_WORKS = "https://api.crossref.org/works"
_PAGE_SIZE = 100
_OFFSET_WINDOW = 10_000  # Crossref's maximum result window for offset paging.


def crossref_search(
    query: str,
    limit: int,
    *,
    issn: str,
    year_from: int | None,
    year_to: int | None,
    types: list[str],
    author: str,
    sort: str | None,
    raw_filter: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"rows": _PAGE_SIZE}
    if query:
        params["query"] = query
    if author:
        params["query.author"] = author
    if sort:
        params.update(sort="issued", order=sort.partition(":")[2] or "desc")
    if config.get("email"):
        params["mailto"] = config["email"]
    clauses = []
    if issn:
        clauses.append(f"issn:{issn}")
    if year_from is not None:
        clauses.append(f"from-pub-date:{year_from:04d}-01-01")
    if year_to is not None:
        clauses.append(f"until-pub-date:{year_to:04d}-12-31")
    clauses.extend(f"type:{kind}" for kind in types)
    if raw_filter:
        clauses.append(raw_filter)
    if clauses:
        params["filter"] = ",".join(clauses)

    results: list[dict[str, Any]] = []
    seen = set()
    # Unlike cursor paging, offset preserves relevance and supports issued ordering.
    for offset in range(0, _OFFSET_WINDOW, _PAGE_SIZE):
        page = _works_page({**params, "offset": offset}, config)
        items = page["items"]
        for item in items:
            doi = normalize_doi(item.get("DOI") or "")
            if not doi or doi.casefold() in seen:
                continue
            seen.add(doi.casefold())
            results.append(_work(item, doi))
            if len(results) == limit:
                return results
        if len(items) < _PAGE_SIZE:
            break
    return results


def _works_page(params: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    try:
        response = request_response(
            _WORKS,
            config,
            session=_get_session(config),
            source="crossref",
            operation="search",
            params=params,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException:
        raise UpstreamError(
            "crossref", "Crossref: request failed (network error)"
        ) from None
    with response:
        status = response.status_code
        if status >= 400:
            detail = _validation_message(response) if status == 400 else ""
            raise UpstreamError(
                "crossref",
                detail or f"Crossref: request failed (HTTP {status})",
                status_code=status,
            )
        try:
            payload = response.json()
        except ValueError:
            raise UpstreamError("crossref", "Crossref: returned invalid JSON") from None
    page = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise UpstreamError("crossref", "Crossref returned an invalid results payload")
    return page


def _validation_message(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return (
            response.text
            if "text/html" not in response.headers.get("Content-Type", "")
            else ""
        )
    detail = body.get("message") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        return "\n".join(
            entry["message"]
            for entry in detail
            if isinstance(entry, dict) and isinstance(entry.get("message"), str)
        )
    return ""


def crossref_abstract(text: str) -> str:
    """Strip JATS markup and the bare Abstract heading from Crossref text."""
    return re.sub(
        r"^Abstract\s+", "", " ".join(unescape(re.sub(r"<[^>]+>", " ", text)).split())
    )


def _work(item: dict[str, Any], doi: str) -> dict[str, Any]:
    year = publication_date = None
    parts = ((item.get("issued") or {}).get("date-parts") or [[]])[0]
    if parts and isinstance(parts[0], int):
        year = parts[0]
        if len(parts) == 3:
            try:
                publication_date = date(*parts).isoformat()
            except (TypeError, ValueError):
                pass
    authors = []
    for author in (item.get("author") or [])[:5]:
        name = " ".join(filter(None, (author.get("given"), author.get("family"))))
        if name := name or author.get("name"):
            authors.append(name)
    title = (item.get("title") or [""])[0]
    return {
        "doi": doi,
        "title": unescape(re.sub(r"<[^>]+>", "", title)).strip(),
        "url": item.get("URL"),
        "authors": authors,
        "year": year,
        "publication_date": publication_date,
        "journal": (item.get("container-title") or [None])[0],
        "cited_by_count": item.get("is-referenced-by-count"),
        "abstract": crossref_abstract(item.get("abstract") or ""),
        "source": "crossref",
        "type": item.get("type"),
    }
