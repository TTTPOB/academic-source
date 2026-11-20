"""Turn public metadata locations into download candidates, without URL suffix gates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from scansci_pdf.pmc_cloud import europepmc_pdf_url
from scansci_pdf.upstream_client import openalex_content_url


@dataclass(frozen=True)
class Candidate:
    url: str
    source: str
    landing_url: str = ""


def pmcid_from_location(value: str) -> str | None:
    """Recognize PMC identifiers and the public NCBI/Europe PMC URL variants."""
    value = value.strip()
    match = re.fullmatch(r"PMC(\d+)", value, re.IGNORECASE)
    if match:
        return f"PMC{match[1]}"
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host not in {
        "ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
        "pmc.ncbi.nlm.nih.gov",
        "europepmc.org",
        "www.europepmc.org",
    }:
        return None
    match = re.search(
        r"/(?:pmc/)?articles/(?:PMC)?(\d+)(?:/|$)", parsed.path, re.IGNORECASE
    )
    if not match:
        match = re.search(
            r"/article/PMC/(?:PMC)?(\d+)(?:/|$)", parsed.path, re.IGNORECASE
        )
    if match:
        return f"PMC{match[1]}"
    for key, values in parse_qs(parsed.query).items():
        if key.lower() == "pmcid":
            return pmcid_from_location(values[0])
    return None


def expand_candidate(candidate: Candidate) -> list[Candidate]:
    """Use getPdf for PMC locations instead of HTML viewers or landing pages."""
    pmcid = pmcid_from_location(candidate.url)
    return [Candidate(europepmc_pdf_url(pmcid), "EuropePMC")] if pmcid else [candidate]


def _location(pdf: Any, landing: Any, source: str) -> list[Candidate]:
    candidates = []
    if isinstance(pdf, str) and pdf:
        candidates.append(Candidate(pdf, source, landing or ""))
    if isinstance(landing, str) and landing:
        candidates.append(Candidate(landing, source, landing))
    return candidates


def unpaywall_candidates(payload: dict[str, Any]) -> list[Candidate]:
    locations = [payload.get("best_oa_location"), *(payload.get("oa_locations") or [])]
    locations = [item for item in locations if isinstance(item, dict)]
    locations.sort(key=lambda item: item.get("host_type") == "publisher")
    return [
        candidate
        for item in locations
        for candidate in _location(
            item.get("url_for_pdf"),
            item.get("url_for_landing_page") or item.get("url"),
            "Unpaywall",
        )
    ]


def openalex_candidates(payload: dict[str, Any], api_key: str = "") -> list[Candidate]:
    locations = [
        payload.get("best_oa_location"),
        payload.get("primary_location"),
        *(payload.get("locations") or []),
    ]
    candidates = []
    for item in locations:
        if not isinstance(item, dict):
            continue
        # A declared PDF is evidence worth testing even when the OA flag is stale.
        if (
            item.get("is_oa")
            or item.get("pdf_url")
            or pmcid_from_location(item.get("landing_page_url") or "")
        ):
            candidates.extend(
                _location(
                    item.get("pdf_url"), item.get("landing_page_url"), "OpenAlexOA"
                )
            )
    oa = payload.get("open_access") or {}
    if oa.get("oa_url"):
        candidates.append(Candidate(oa["oa_url"], "OpenAlexOA"))
    if api_key and oa.get("is_oa") and payload.get("id"):
        work_id = payload["id"].rsplit("/", 1)[-1]
        candidates.append(
            Candidate(
                openalex_content_url(work_id, {"openalex_api_key": api_key}),
                "OpenAlexContent",
            )
        )
    return candidates
