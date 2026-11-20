"""Offline discovery tests using trimmed publisher responses."""

from pathlib import Path

import pytest

from scansci_pdf.publisher_strategies import StrategyRegistry
from scansci_pdf.supplementary import SupplementaryItem

FIXTURES = Path(__file__).parent / "fixtures" / "supplementary"
NATURE_DOI = "10.1038/s41467-024-50600-w"
NATURE_PAGE = "https://www.nature.com/articles/s41467-024-50600-w"
NATURE_FILES = "https://static-content.springer.com/esm/art%3A10.1038%2Fs41467-024-50600-w/MediaObjects/"
ELSEVIER_DOI = "10.1016/j.celrep.2023.112768"
ELSEVIER_API = "https://api.elsevier.com/content"
ELSEVIER_PAGE = f"{ELSEVIER_API}/article/doi/{ELSEVIER_DOI}"


def test_nature_article_discovers_only_listed_attachments():
    strategy = StrategyRegistry.get_for_doi(NATURE_DOI)
    assert strategy is not None
    assert strategy.supplementary_pages(NATURE_DOI) == [f"https://doi.org/{NATURE_DOI}"]
    html = (FIXTURES / "nature-moesm.html").read_text(encoding="utf-8")
    expected = [
        ("Supplementary Information", "41467_2024_50600_MOESM1_ESM.pdf"),
        ("Peer Review File", "41467_2024_50600_MOESM2_ESM.pdf"),
        ("Reporting Summary", "41467_2024_50600_MOESM3_ESM.pdf"),
        ("Source Data", "41467_2024_50600_MOESM4_ESM.xlsx"),
    ]
    assert strategy.supplementary_items(html, NATURE_PAGE) == [
        SupplementaryItem(NATURE_FILES + filename, label, "", filename)
        for label, filename in expected
    ]
    assert (
        strategy.supplementary_items(
            '<a href="article.pdf">Download PDF</a>', NATURE_PAGE
        )
        == []
    )


def test_nature_caption_and_relative_path():
    # The captured Nature sample only has titles; exercise an item with captions separately.
    strategy = StrategyRegistry.get_for_doi(NATURE_DOI)
    assert strategy is not None
    html = """<div class="c-article-supplementary__item">
      <h3 class="c-article-supplementary__title"><a href="/media/123_MOESM1_ESM.zip">
        Supplementary Data 1 (download ZIP)</a></h3>
      <p>First caption, including <em>inline text</em> and punctuation.</p>
      <p>Second paragraph.</p>
    </div>"""
    assert strategy.supplementary_items(html, NATURE_PAGE) == [
        SupplementaryItem(
            "https://www.nature.com/media/123_MOESM1_ESM.zip",
            "Supplementary Data 1",
            "First caption, including inline text and punctuation.\nSecond paragraph.",
            "123_MOESM1_ESM.zip",
        )
    ]


def _elsevier_item(pii, filename, label, description=""):
    return SupplementaryItem(
        f"{ELSEVIER_API}/object/eid/1-s2.0-{pii}-{filename}",
        label,
        description,
        filename,
    )


def test_elsevier_cell_reports_attachment_metadata():
    strategy = StrategyRegistry.get_for_doi(ELSEVIER_DOI)
    assert strategy is not None
    assert strategy.supplementary_pages(ELSEVIER_DOI) == [
        ELSEVIER_PAGE,
        f"https://doi.org/{ELSEVIER_DOI}",
    ]
    xml = (FIXTURES / "elsevier-cell-reports.xml").read_text(encoding="utf-8")
    pii = "S2211124723007799"
    assert strategy.supplementary_items(xml, ELSEVIER_PAGE) == [
        _elsevier_item(
            pii, "mmc1.pdf", "Document S1. Figures\u00a0S1–S7 and Table\u00a0S1"
        ),
        _elsevier_item(
            pii,
            "mmc2.xlsx",
            "Table\u00a0S2. N and p values, related to Figures\u00a01–6",
        ),
        _elsevier_item(
            pii, "mmc3.pdf", "Document S2. Article plus supplemental information"
        ),
    ]


def test_elsevier_molecular_cell_retains_caption_and_prefers_mp4():
    doi = "10.1016/j.molcel.2017.10.001"
    strategy = StrategyRegistry.get_for_doi(doi)
    assert strategy is not None
    xml = (FIXTURES / "elsevier-molecular-cell.xml").read_text(encoding="utf-8")
    items = strategy.supplementary_items(xml, f"{ELSEVIER_API}/article/doi/{doi}")
    assert items is not None
    assert [item.filename for item in items] == [
        "mmc1.pdf",
        "mmc2.mp4",
        "mmc3.mp4",
        "mmc4.mp4",
        "mmc5.pdf",
    ]
    assert items[1] == _elsevier_item(
        "S1097276517307505",
        "mmc2.mp4",
        "Movie S1. Time-Lapse Imaging of Fucci Variants",
        "Related to Figure\u00a02. HeLa/Fucci(SA)2, HeLa/Fucci(CA)2, and HeLa/Fucci(SCA)2 "
        "were grown on glass-bottom dishes, and time-lapse imaging was performed using an "
        "LCV100 microscope. Images were acquired every 12.5\u00a0min. Total imaging time\u00a0= 96\u00a0hr.",
    )


def test_elsevier_main_pdf_is_not_a_supplement():
    strategy = StrategyRegistry.get_for_doi(ELSEVIER_DOI)
    assert strategy is not None
    xml = """<full-text-retrieval-response><attachments><attachment>
      <attachment-eid>1-s2.0-S2211124723007799-main.pdf</attachment-eid>
      <filename>main.pdf</filename>
    </attachment></attachments></full-text-retrieval-response>"""
    assert strategy.supplementary_items(xml, ELSEVIER_PAGE) == []


def test_elsevier_unexpected_api_response_allows_web_fallback():
    strategy = StrategyRegistry.get_for_doi(ELSEVIER_DOI)
    assert strategy is not None
    assert strategy.supplementary_items("<html>Not XML</html>", ELSEVIER_PAGE) is None


@pytest.mark.parametrize("host", ["www.sciencedirect.com", "www.cell.com"])
def test_elsevier_web_page_uses_generic_discovery(host):
    strategy = StrategyRegistry.get_for_doi(ELSEVIER_DOI)
    assert strategy is not None
    assert (
        strategy.supplementary_items("<html></html>", f"https://{host}/article") is None
    )


def test_elsevier_metadata_only_api_response_allows_web_fallback():
    strategy = StrategyRegistry.get_for_doi(ELSEVIER_DOI)
    assert strategy is not None
    xml = "<full-text-retrieval-response><coredata/></full-text-retrieval-response>"
    assert strategy.supplementary_items(xml, ELSEVIER_PAGE) is None
