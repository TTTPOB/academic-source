"""Fill in abstracts from PubMed, which has them for most biomedical papers."""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

import requests

from .network import _get_session
from .upstream_client import request_response
from .upstream_errors import UpstreamError

_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
# Keeps each search URL to a few kilobytes.
_BATCH = 100


def pubmed_abstracts(dois: list[str], config: dict[str, Any]) -> dict[str, str]:
    """Map lower-cased DOI to abstract for the DOIs PubMed knows; failures yield none."""
    found: dict[str, str] = {}
    unique = list(dict.fromkeys(doi.lower() for doi in dois if doi))
    for start in range(0, len(unique), _BATCH):
        batch = unique[start : start + _BATCH]
        try:
            found.update(_batch(batch, config))
        except (UpstreamError, requests.RequestException, ValueError, ElementTree.ParseError):
            # Abstracts are optional; the caller already has its records.
            continue
    return found


def _get(path: str, params: dict[str, Any], config: dict[str, Any]) -> bytes:
    response = request_response(
        _EUTILS + path,
        config,
        session=_get_session(config),
        params={**params, "tool": "academic-source"},
        source="pubmed",
        operation="lookup",
    )
    with response:
        response.raise_for_status()
        return response.content


def _batch(dois: list[str], config: dict[str, Any]) -> dict[str, str]:
    import json

    term = " OR ".join(f'"{doi}"[doi]' for doi in dois)
    payload = json.loads(
        _get(
            "esearch.fcgi",
            {"db": "pubmed", "term": term, "retmax": len(dois) * 2, "retmode": "json"},
            config,
        )
    )
    ids = payload.get("esearchresult", {}).get("idlist") or []
    if not ids:
        return {}
    root = ElementTree.fromstring(
        _get("efetch.fcgi", {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}, config)
    )
    wanted = set(dois)
    found = {}
    for article in root.iter("PubmedArticle"):
        doi = next(
            (
                (item.text or "").strip().lower()
                for item in article.iter("ArticleId")
                if item.get("IdType") == "doi"
            ),
            "",
        )
        if doi not in wanted:
            continue
        parts = []
        for section in article.iter("AbstractText"):
            text = " ".join("".join(section.itertext()).split())
            label = section.get("Label")
            if text:
                parts.append(f"{label}: {text}" if label else text)
        if parts:
            found[doi] = " ".join(parts)
    return found
