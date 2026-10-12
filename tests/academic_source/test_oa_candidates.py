"""Pure OA location parsing regressions, without network or browser I/O."""

import pytest

from academic_source.sources.oa_candidates import (
    Candidate,
    europepmc_candidates,
    expand_candidate,
    openalex_candidates,
    pmcid_from_location,
    semantic_scholar_candidates,
    unpaywall_candidates,
)


@pytest.mark.parametrize(
    "value",
    [
        "pmc134036",
        "https://www.ncbi.nlm.nih.gov/pmc/articles/134036",
        "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC134036/pdf/",
        "https://pmc.ncbi.nlm.nih.gov/articles/pMc134036/",
        "https://europepmc.org/articles/PMC134036?pdf=render",
        "https://europepmc.org/ARTICLE/pmc/134036",
        "https://europepmc.org/api/getPdf?PMCID=pmc134036",
    ],
)
def test_pmc_variants_resolve_to_website_pdf_endpoint(value):
    assert pmcid_from_location(value) == "PMC134036"
    assert expand_candidate(Candidate(value, "Metadata"))[0].url == (
        "https://europepmc.org/api/getPdf?pmcid=PMC134036"
    )


def test_ncbi_aliases_share_canonical_candidate_and_keep_query():
    old = expand_candidate(
        Candidate("https://www.ncbi.nlm.nih.gov/pmc/articles/134036?pdf=render", "OA")
    )
    new = expand_candidate(
        Candidate("https://pmc.ncbi.nlm.nih.gov/articles/pmc134036/?pdf=render", "OA")
    )
    assert [item.url for item in old] == [item.url for item in new]
    assert old[-1].url.endswith("/PMC134036/?pdf=render")


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


def test_semantic_scholar_retains_external_pmc_and_arxiv_ids():
    candidates = semantic_scholar_candidates(
        {"externalIds": {"PubMedCentral": "134036", "ArXiv": "2401.01234v2"}}
    )
    assert [item.url for item in candidates] == [
        "PMC134036",
        "https://arxiv.org/pdf/2401.01234v2.pdf",
    ]


def test_europepmc_free_pdf_does_not_require_cc_or_pdf_suffix():
    payload = {
        "resultList": {
            "result": [
                {
                    "isOpenAccess": "N",
                    "fullTextUrlList": {
                        "fullTextUrl": [
                            {
                                "url": "https://repo.org/api/getFile?id=42",
                                "documentStyle": "pdf",
                                "availability": "FreePDF",
                            },
                            {
                                "url": "https://repo.org/landing",
                                "documentStyle": "html",
                                "availability": "Free",
                            },
                        ]
                    },
                }
            ]
        }
    }
    assert [item.url for item in europepmc_candidates(payload)] == [
        "https://repo.org/api/getFile?id=42",
        "https://repo.org/landing",
    ]
