"""Turn public metadata locations into download candidates, without URL suffix gates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit


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
    """Try the website PDF endpoint before a repository landing/viewer URL."""
    pmcid = pmcid_from_location(candidate.url)
    if not pmcid:
        return [candidate]
    direct = Candidate(
        f"https://europepmc.org/api/getPdf?pmcid={pmcid}",
        "EuropePMC",
        f"https://europepmc.org/articles/{pmcid}",
    )
    if candidate.url.upper().startswith("PMC"):
        landing = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
        return [
            direct,
            Candidate(direct.landing_url, candidate.source),
            Candidate(landing + "pdf/", candidate.source, landing),
        ]
    parsed = urlsplit(candidate.url)
    if (parsed.hostname or "").lower().endswith("ncbi.nlm.nih.gov"):
        match = re.search(r"/(?:pmc/)?articles/(?:PMC)?\d+", parsed.path, re.IGNORECASE)
        if match:
            path = f"/articles/{pmcid}/" + parsed.path[match.end() :].lstrip("/")
            canonical = parsed._replace(
                scheme="https", netloc="pmc.ncbi.nlm.nih.gov", path=path, fragment=""
            ).geturl()
            candidate = Candidate(
                canonical,
                candidate.source,
                f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
            )
    return [direct, candidate]


def _location(pdf: Any, landing: Any, source: str) -> list[Candidate]:
    candidates = []
    if isinstance(pdf, str) and pdf:
        candidates.append(Candidate(pdf, source, landing or ""))
    if isinstance(landing, str) and landing:
        candidates.append(Candidate(landing, source))
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
                f"https://content.openalex.org/works/{work_id}.pdf?mailto={api_key}",
                "OpenAlexContent",
            )
        )
    return candidates


def semantic_scholar_candidates(payload: dict[str, Any]) -> list[Candidate]:
    candidates = []
    ids = payload.get("externalIds") or {}
    for key, value in ids.items():
        if key.lower() in {"pubmedcentral", "pmcid", "pmc"} and value:
            pmcid = str(value)
            if pmcid.isdigit():
                pmcid = f"PMC{pmcid}"
            candidates.append(Candidate(pmcid, "SemanticScholar"))
    pdf = payload.get("openAccessPdf") or {}
    if pdf.get("url"):
        candidates.append(Candidate(pdf["url"], "SemanticScholar"))
    if ids.get("ArXiv"):
        candidates.append(
            Candidate(
                f"https://arxiv.org/pdf/{ids['ArXiv']}.pdf", "SemanticScholar(arXiv)"
            )
        )
    return candidates


def europepmc_candidates(payload: dict[str, Any]) -> list[Candidate]:
    candidates = []
    for item in payload.get("resultList", {}).get("result", []):
        if item.get("pmcid"):
            candidates.append(Candidate(item["pmcid"], "EuropePMC"))
        entries = item.get("fullTextUrlList", {}).get("fullTextUrl", [])
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            url = entry.get("url") or ""
            # Free to read is not a CC license; explicit FreePDF still works with isOpenAccess=N.
            if url and (
                str(entry.get("documentStyle", "")).lower() == "pdf"
                or str(entry.get("availability", "")).lower()
                in {"free", "freepdf", "open access"}
                or pmcid_from_location(url)
            ):
                candidates.append(Candidate(url, "EuropePMC"))
    return candidates


def pmc_candidates(payload: dict[str, Any]) -> list[Candidate]:
    return [
        Candidate(item["pmcid"], "PMC")
        for item in payload.get("records", [])
        if item.get("pmcid")
    ]
