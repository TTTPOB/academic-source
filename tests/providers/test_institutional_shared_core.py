"""Keep institutional publisher routing on the shared implementation."""

import pytest

from scansci_pdf import publisher_pdf_router, publisher_profiles
from scansci_pdf.extractors import html_extractor, pdf_extractor
from scansci_pdf.extractors.publisher_adapters import nature
from scansci_pdf.institutional import publisher_pdf_router as institutional_router
from scansci_pdf.institutional import publisher_profiles as institutional_profiles
from scansci_pdf.institutional.extractors import html_extractor as institutional_html
from scansci_pdf.institutional.extractors import pdf_extractor as institutional_pdf
from scansci_pdf.institutional.extractors.publisher_adapters import (
    nature as institutional_nature,
)


@pytest.mark.parametrize(
    ("doi", "source_url", "expected"),
    [
        (
            "10.1016/j.cell.2024.01.001",
            "https://www.sciencedirect.com/science/article/pii/S0092867424000012",
            "https://www.sciencedirect.com/science/article/pii/S0092867424000012/pdfft",
        ),
        (
            "10.1103/PhysRevLett.123.456789",
            "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.123.456789",
            "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.123.456789",
        ),
        (
            "10.5194/acp-22-123-2022",
            "https://acp.copernicus.org/articles/22/123/2022/",
            "https://acp.copernicus.org/articles/22/123/2022/acp-22-123-2022.pdf",
        ),
    ],
)
def test_institutional_profile_routes_to_publisher_pdf(doi, source_url, expected):
    profile = institutional_profiles.infer_publisher_profile(doi)
    assert profile is not None
    assert (
        institutional_router.build_pdf_candidates(profile, doi, source_url=source_url)[
            0
        ]
        == expected
    )


def test_institutional_html_adapter_extracts_nature_title_and_authors():
    html = """<html><head><meta name="citation_author" content="Ada Lovelace"></head>
    <body><h1 class="c-article-title">A useful discovery</h1></body></html>"""
    result = institutional_html.extract(html, "https://www.nature.com/articles/example")
    assert result["title"] == "A useful discovery"
    assert result["authors"] == ["Ada Lovelace"]


def test_institutional_exports_share_implementations():
    assert institutional_profiles.ACS_PROFILE is publisher_profiles.ACS_PROFILE
    assert (
        institutional_router.build_pdf_candidates
        is publisher_pdf_router.build_pdf_candidates
    )
    assert institutional_html.extract is html_extractor.extract
    assert institutional_pdf.extract_text is pdf_extractor.extract_text
    assert institutional_nature.extract is nature.extract
    assert "id.tsinghua.edu.cn" in institutional_profiles.ACS_PROFILE.auth_url_markers
