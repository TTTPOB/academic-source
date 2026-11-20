"""Download full-text PDFs from Europe PMC and PMC Cloud."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from ..network import fetch, polite_delay
from ..pdf_utils import download_pdf
from ..pmc_cloud import europepmc_pdf_url, find_article, lookup_pmcid


def try_europepmc(
    doi: str, output_path: Path, config: dict[str, Any]
) -> dict[str, Any] | None:
    pmcid = lookup_pmcid(
        doi, partial(fetch, config=config, headers={"Accept": "application/json"})
    )
    if not pmcid:
        return None
    polite_delay(config)
    result = download_pdf(
        europepmc_pdf_url(pmcid),
        output_path,
        config,
        "EuropePMC",
        require_pdf_like_url=False,
    )
    if result:
        result.update(doi=doi, identifier=doi)
    return result


def try_pmc(
    doi: str, output_path: Path, config: dict[str, Any]
) -> dict[str, Any] | None:
    pmcid = lookup_pmcid(
        doi, partial(fetch, config=config, headers={"Accept": "application/json"})
    )
    if not pmcid:
        return None
    article = find_article(pmcid, partial(fetch, config=config))
    pdf = article.pdf if article else None
    if not pdf:
        return None
    result = download_pdf(pdf.url, output_path, config, f"PMC({pmcid})")
    if result:
        result.update(doi=doi, identifier=doi)
    return result
