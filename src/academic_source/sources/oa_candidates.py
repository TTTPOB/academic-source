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
    # Submitted before peer review; tried only after every other version fails.
    preprint: bool = False


# Hosts that serve preprints, used when a location does not state its version.
_PREPRINT_HOSTS = (
    "arxiv.org",
    "biorxiv.org",
    "chemrxiv.org",
    "medrxiv.org",
    "osf.io",
    "preprints.org",
    "researchsquare.com",
    "ssrn.com",
)
# DOI prefixes of preprint servers: bioRxiv/medRxiv (new prefix), Research Square,
# SSRN, Preprints.org, ChemRxiv, OSF Preprints, arXiv.
_PREPRINT_DOI_PREFIXES = (
    "10.64898/",
    "10.21203/",
    "10.2139/",
    "10.20944/",
    "10.26434/",
    "10.31219/",
    "10.48550/",
)
# 10.1101 is shared with Cold Spring Harbor journals (10.1101/gad..., 10.1101/gr...);
# bioRxiv and medRxiv suffixes are dates (2022.04.19.488821) or bare numbers (199380).
_BIORXIV_SUFFIX = re.compile(r"(?:\d{4}\.\d{2}\.\d{2}\.)?\d+(?:v\d+)?")


def is_preprint_doi(doi: str) -> bool:
    doi = doi.strip().lower()
    if doi.startswith("10.1101/"):
        return bool(_BIORXIV_SUFFIX.fullmatch(doi.removeprefix("10.1101/")))
    return doi.startswith(_PREPRINT_DOI_PREFIXES)


def _is_preprint(version: Any, *urls: Any) -> bool:
    if version == "submittedVersion":
        return True
    if version in ("publishedVersion", "acceptedVersion"):
        return False
    for url in urls:
        host = (urlsplit(url).hostname or "").lower() if isinstance(url, str) else ""
        if any(host == item or host.endswith("." + item) for item in _PREPRINT_HOSTS):
            return True
    return False


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


def _location(pdf: Any, landing: Any, source: str, version: Any) -> list[Candidate]:
    preprint = _is_preprint(version, pdf, landing)
    candidates = []
    if isinstance(pdf, str) and pdf:
        candidates.append(Candidate(pdf, source, landing or "", preprint))
    if isinstance(landing, str) and landing:
        candidates.append(Candidate(landing, source, landing, preprint))
    return candidates


def unpaywall_candidates(payload: dict[str, Any]) -> list[Candidate]:
    locations = [payload.get("best_oa_location"), *(payload.get("oa_locations") or [])]
    locations = [item for item in locations if isinstance(item, dict)]
    # Repositories before publisher pages, which are often challenged; preprints last.
    candidates = [
        candidate
        for item in sorted(
            locations, key=lambda item: item.get("host_type") == "publisher"
        )
        for candidate in _location(
            item.get("url_for_pdf"),
            item.get("url_for_landing_page") or item.get("url"),
            "Unpaywall",
            item.get("version"),
        )
    ]
    return sorted(candidates, key=lambda candidate: candidate.preprint)


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
                    item.get("pdf_url"),
                    item.get("landing_page_url"),
                    "OpenAlexOA",
                    item.get("version"),
                )
            )
    oa = payload.get("open_access") or {}
    if oa.get("oa_url"):
        url = oa["oa_url"]
        candidates.append(
            Candidate(url, "OpenAlexOA", preprint=_is_preprint(None, url))
        )
    if api_key and oa.get("is_oa") and payload.get("id"):
        work_id = payload["id"].rsplit("/", 1)[-1]
        candidates.append(
            Candidate(
                openalex_content_url(work_id, {"openalex_api_key": api_key}),
                "OpenAlexContent",
            )
        )
    return candidates
