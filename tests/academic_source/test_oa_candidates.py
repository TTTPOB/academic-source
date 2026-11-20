"""Pure OA location parsing regressions, without network or browser I/O."""

import pytest

from academic_source.sources.oa_candidates import (
    Candidate,
    expand_candidate,
    openalex_candidates,
    pmcid_from_location,
    unpaywall_candidates,
)


@pytest.mark.parametrize(
    "value",
    [
        "pmc134036",
        "https://www.ncbi.nlm.nih.gov/pmc/articles/134036",
        "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC134036/pdf/",
        "https://pmc.ncbi.nlm.nih.gov/articles/pMc134036/",
        "https://europepmc.org/articles/PMC134036",
        "https://europepmc.org/articles/PMC134036?pdf=render",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC134036/pdf/article.pdf",
        "https://europepmc.org/ARTICLE/pmc/134036",
        "https://europepmc.org/api/getPdf?PMCID=pmc134036",
    ],
)
def test_pmc_variants_resolve_only_to_website_pdf_endpoint(value):
    assert pmcid_from_location(value) == "PMC134036"
    assert expand_candidate(Candidate(value, "Metadata")) == [
        Candidate("https://europepmc.org/api/getPdf?pmcid=PMC134036", "EuropePMC")
    ]


def test_unpaywall_preserves_pdf_and_landing_even_without_pdf_suffix():
    payload = {
        "best_oa_location": {
            "host_type": "repository",
            "url_for_pdf": "https://repo.org/file?id=42",
            "url_for_landing_page": "https://repo.org/article/42",
        },
        "oa_locations": [{"url": "https://europepmc.org/articles/PMC134036"}],
    }
    candidates = unpaywall_candidates(payload)
    assert [item.url for item in candidates] == [
        "https://repo.org/file?id=42",
        "https://repo.org/article/42",
        "https://europepmc.org/articles/PMC134036",
    ]
    assert candidates[0].landing_url == candidates[1].url


def test_openalex_includes_public_locations_not_just_primary():
    payload = {
        "open_access": {"is_oa": False},
        "primary_location": {"is_oa": False, "landing_page_url": "https://paywall.org"},
        "locations": [
            {"is_oa": True, "landing_page_url": "https://repo.org/item/42"},
            {"is_oa": False, "pdf_url": "https://repo.org/api/file?id=42"},
        ],
    }
    assert [item.url for item in openalex_candidates(payload)] == [
        "https://repo.org/item/42",
        "https://repo.org/api/file?id=42",
    ]


def test_openalex_repository_locations_retain_pmc_and_versioned_arxiv():
    candidates = openalex_candidates(
        {
            "locations": [
                {
                    "is_oa": False,
                    "landing_page_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC134036/",
                },
                {
                    "is_oa": True,
                    "pdf_url": "https://arxiv.org/pdf/2401.01234v2.pdf",
                    "landing_page_url": "https://arxiv.org/abs/2401.01234v2",
                },
            ]
        }
    )
    assert [item.url for item in expand_candidate(candidates[0])][:1] == [
        "https://europepmc.org/api/getPdf?pmcid=PMC134036"
    ]
    assert [item.url for item in candidates[1:]] == [
        "https://arxiv.org/pdf/2401.01234v2.pdf",
        "https://arxiv.org/abs/2401.01234v2",
    ]
    assert candidates[1].landing_url == candidates[2].url
