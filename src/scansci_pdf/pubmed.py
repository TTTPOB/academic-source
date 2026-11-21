"""Search PubMed and look up optional abstracts by DOI via E-utilities."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any
from xml.etree import ElementTree

import requests

from .identifiers import normalize_doi
from .network import _get_session
from .upstream_client import request_response
from .upstream_errors import UpstreamError

_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
_BATCH = 100
_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


def pubmed_search(
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
    clauses = [query] if query else []
    if issn:
        clauses.append(f"{issn}[issn]")
    if year_from is not None or year_to is not None:
        if year_from == year_to:
            clauses.append(f"{year_from:04d}[dp]")
        else:
            clauses.append(f"{year_from or 1:04d}:{year_to or 9999:04d}[dp]")
    if author:
        clauses.append(f"{author}[au]")
    if types:
        clauses.append(" OR ".join(f"{kind}[pt]" for kind in types))
    if raw_filter:
        clauses.append(raw_filter)
    # Group clauses to preserve the meaning of a native OR expression under AND.
    term = (
        clauses[0]
        if len(clauses) == 1
        else " AND ".join(f"({clause})" for clause in clauses)
    )
    ids = list(
        dict.fromkeys(
            _search_ids(
                term,
                limit,
                config,
                by_date=bool(sort or not (query or author)),
                report_query_warnings=True,
            )
        )
    )[:limit]
    results = []
    for start in range(0, len(ids), _BATCH):
        batch = ids[start : start + _BATCH]
        records = {}
        for article in _fetch_articles(batch, config):
            if record := _record(article):
                records[record["pmid"]] = record
        # EFetch record order need not match ESearch order, especially for books.
        results.extend(records[pmid] for pmid in batch if pmid in records)
    return results


def pubmed_abstracts(dois: list[str], config: dict[str, Any]) -> dict[str, str]:
    """Map lower-cased DOI to abstract for the DOIs PubMed knows; failures yield none."""
    found: dict[str, str] = {}
    unique = list(dict.fromkeys(doi.lower() for doi in dois if doi))
    for start in range(0, len(unique), _BATCH):
        batch = unique[start : start + _BATCH]
        try:
            found.update(_batch(batch, config))
        except (
            UpstreamError,
            requests.RequestException,
            ValueError,
            ElementTree.ParseError,
        ):
            # Abstracts are optional; the caller already has its records.
            continue
    return found


def _get(path: str, params: dict[str, Any], config: dict[str, Any]) -> bytes:
    # POST: a hundred DOIs make a query string longer than E-utilities accepts.
    try:
        response = request_response(
            _EUTILS + path,
            config,
            session=_get_session(config),
            method="POST",
            data={**params, "tool": "academic-source"},
            source="pubmed",
            operation="lookup",
        )
    except requests.RequestException:
        raise UpstreamError(
            "pubmed", "PubMed: request failed (network error)"
        ) from None
    with response:
        status = response.status_code
        if status >= 400:
            detail = _error_detail(response) if status == 400 else ""
            raise UpstreamError(
                "pubmed",
                detail or f"PubMed: request failed (HTTP {status})",
                status_code=status,
            )
        return response.content


def _error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError:
            return (
                response.text
                if "text/html" not in response.headers.get("Content-Type", "")
                else ""
            )
        return _xml_error(root) if root.tag.lower() != "html" else ""
    return _json_error(payload)


def _json_error(payload: Any) -> str:
    if isinstance(payload, dict):
        detail = payload.get("error") or payload.get("ERROR") or payload.get("message")
        return detail if isinstance(detail, str) else ""
    return ""


def _xml_error(root: ElementTree.Element) -> str:
    error = root if root.tag == "ERROR" else root.find(".//ERROR")
    return "".join(error.itertext()) if error is not None else ""


def _search_ids(
    term: str,
    limit: int,
    config: dict[str, Any],
    *,
    by_date: bool = False,
    report_query_warnings: bool = False,
) -> list[str]:
    params = {"db": "pubmed", "term": term, "retmax": limit, "retmode": "json"}
    if by_date:
        params["sort"] = "pub date"
    try:
        payload = json.loads(_get("esearch.fcgi", params, config))
    except ValueError:
        raise UpstreamError("pubmed", "PubMed: returned invalid JSON") from None
    result = payload.get("esearchresult") if isinstance(payload, dict) else None
    error = _json_error(payload) or _json_error(result)
    if error:
        raise UpstreamError("pubmed", error, status_code=400)
    if not isinstance(result, dict) or not isinstance(
        ids := result.get("idlist"), list
    ):
        raise UpstreamError("pubmed", "PubMed returned an invalid ESearch payload")
    if report_query_warnings:
        _raise_query_warnings(result, has_matches=bool(ids))
    return ids


def _raise_query_warnings(result: dict[str, Any], *, has_matches: bool) -> None:
    """Report ESearch's diagnostics without interpreting the submitted expression."""
    errors = result.get("errorlist") or {}
    warnings = result.get("warninglist") or {}
    messages = [
        message
        for message in warnings.get("outputmessages", [])
        if message != "No items found."
    ]
    for key, values in (
        ("fieldsnotfound", errors.get("fieldsnotfound", [])),
        ("phrasesnotfound", errors.get("phrasesnotfound", []) if has_matches else []),
        ("phrasesignored", warnings.get("phrasesignored", []) if has_matches else []),
        (
            "quotedphrasesnotfound",
            warnings.get("quotedphrasesnotfound", []) if has_matches else [],
        ),
    ):
        messages.extend(f"{key}: {value}" for value in values)
    if messages:
        if translation := result.get("querytranslation"):
            messages.append(f"querytranslation: {translation}")
        raise UpstreamError("pubmed", "\n".join(messages), status_code=400)


def _fetch_articles(ids: list[str], config: dict[str, Any]) -> ElementTree.Element:
    try:
        root = ElementTree.fromstring(
            _get(
                "efetch.fcgi",
                {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
                config,
            )
        )
    except ElementTree.ParseError:
        raise UpstreamError("pubmed", "PubMed: returned invalid XML") from None
    if error := _xml_error(root):
        raise UpstreamError("pubmed", error, status_code=400)
    if root.tag != "PubmedArticleSet":
        raise UpstreamError("pubmed", "PubMed returned an invalid EFetch payload")
    return root


def _batch(dois: list[str], config: dict[str, Any]) -> dict[str, str]:
    term = " OR ".join(f'"{doi}"[doi]' for doi in dois)
    ids = _search_ids(term, len(dois) * 2, config)
    if not ids:
        return {}
    wanted = set(dois)
    found = {}
    for article in _fetch_articles(ids, config).iter("PubmedArticle"):
        doi = _article_doi(article).lower()
        if doi in wanted and (abstract := _abstract(article)):
            found[doi] = abstract
    return found


def _text(element: ElementTree.Element, path: str) -> str:
    node = element.find(path)
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _abstract(article: ElementTree.Element) -> str:
    parts = []
    for section in article.iter("AbstractText"):
        text = " ".join("".join(section.itertext()).split())
        label = section.get("Label")
        if text:
            parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts)


def _article_doi(article: ElementTree.Element) -> str:
    return normalize_doi(
        _text(article, ".//ArticleIdList/ArticleId[@IdType='doi']")
        or _text(article, ".//Article/ELocationID[@EIdType='doi']")
    )


def _publication_date(
    publication: ElementTree.Element | None,
) -> tuple[int | None, str | None]:
    if publication is None:
        return None, None
    year = publication.findtext("Year", "")
    if not year.isdigit():
        match = re.match(r"[0-9]{4}", publication.findtext("MedlineDate", ""))
        return (int(match[0]) if match else None), None
    month = publication.findtext("Month", "")
    day = publication.findtext("Day", "")
    try:
        number = int(month) if month.isdigit() else _MONTHS.index(month[:3].lower()) + 1
        return int(year), date(int(year), number, int(day)).isoformat()
    except ValueError:
        return int(year), None


def _record(article: ElementTree.Element) -> dict[str, Any]:
    citation = article.find("MedlineCitation")
    if citation is not None:
        work = citation.find("Article")
        journal = _text(citation, "Article/Journal/Title") or _text(
            citation, "Article/Journal/ISOAbbreviation"
        )
        electronic_date = _publication_date(
            citation.find("Article/ArticleDate[@DateType='Electronic']")
        )
        year, published = (
            electronic_date
            if electronic_date[1]
            else _publication_date(
                citation.find("Article/Journal/JournalIssue/PubDate")
            )
        )
    else:
        citation = work = article.find("BookDocument")
        if citation is None:
            return {}
        journal = _text(citation, "Book/BookTitle")
        year, published = _publication_date(citation.find("Book/PubDate"))
    pmid = _text(citation, "PMID")
    if not pmid or work is None:
        return {}
    authors = []
    for author in work.findall("AuthorList/Author")[:5]:
        name = " ".join(
            filter(
                None,
                (
                    _text(author, "ForeName") or _text(author, "Initials"),
                    _text(author, "LastName"),
                    _text(author, "Suffix"),
                ),
            )
        )
        if name := name or _text(author, "CollectiveName"):
            authors.append(name)
    return {
        "pmid": pmid,
        "doi": _article_doi(article),
        "title": _text(work, "ArticleTitle") or _text(work, "Book/BookTitle"),
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "journal": journal,
        "authors": authors,
        "year": year,
        "publication_date": published,
        "abstract": _abstract(work) or _abstract(citation),
        "source": "pubmed",
        "type": _text(work, "PublicationTypeList/PublicationType")
        or _text(work, "PublicationType"),
    }
