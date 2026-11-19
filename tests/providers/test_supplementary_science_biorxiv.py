from pathlib import Path

from scansci_pdf.publisher_strategies import StrategyRegistry
from scansci_pdf.supplementary import SupplementaryItem

FIXTURES = Path(__file__).parent / "fixtures" / "supplementary"
BIO_DOI = "10.1101/2024.12.23.630206"
BIO = "https://www.biorxiv.org"
MED = "https://www.medrxiv.org"
SCIENCE_DOI = "10.1126/science.adk4858"
SCIENCE = "https://www.science.org"


def test_biorxiv_supplementary_pages() -> None:
    strategy = StrategyRegistry.get_for_doi(BIO_DOI)
    assert strategy is not None
    assert strategy.supplementary_pages(BIO_DOI) == [
        f"{BIO}/content/{BIO_DOI}.supplementary-material",
        f"{BIO}/content/{BIO_DOI}",
        f"{MED}/content/{BIO_DOI}.supplementary-material",
        f"{MED}/content/{BIO_DOI}",
    ]


def test_biorxiv_versioned_supplementary_tab() -> None:
    strategy = StrategyRegistry.get_for_doi(BIO_DOI)
    assert strategy is not None
    html = (FIXTURES / "biorxiv-supplementary.html").read_text(encoding="utf-8")
    labels = [
        "Supplementary Table 1",
        "Supplementary Table 2",
        "Supplementary Table 3",
        "Supplementary Figure 1",
        "Supplementary Figure 2",
    ]
    base = f"{BIO}/content/biorxiv/early/2025/09/29/2024.12.23.630206"
    expected = []
    for index, label in enumerate(labels, 1):
        ext = "xlsx" if index <= 3 else "jpg"
        expected.append(
            SupplementaryItem(
                f"{base}/DC{index}/embed/media-{index}.{ext}",
                label,
                "",
                f"630206_file0{index + 1}.{ext}",
            )
        )
    assert (
        strategy.supplementary_items(
            html, f"{BIO}/content/{BIO_DOI}v3.supplementary-material"
        )
        == expected
    )


def test_biorxiv_legacy_adjunct_files() -> None:
    strategy = StrategyRegistry.get_for_doi("10.1101/342485")
    assert strategy is not None
    html = (FIXTURES / "biorxiv-legacy-supplementary.html").read_text(encoding="utf-8")
    base = f"{BIO}/highwire/filestream/104295/field_highwire_adjunct_files"
    assert strategy.supplementary_items(
        html, f"{BIO}/content/10.1101/342485v1.supplementary-material"
    ) == [
        SupplementaryItem(
            f"{base}/{index - 1}/342485-{index}.xlsx",
            f"Supplementary Table {index}",
            "",
            f"342485-{index}.xlsx",
        )
        for index in range(1, 5)
    ]


def test_medrxiv_caption_and_empty_result() -> None:
    strategy = StrategyRegistry.get_for_doi(BIO_DOI)
    assert strategy is not None
    page = f"{MED}/content/{BIO_DOI}v3.supplementary-material"
    article_assets = f'<a href="/content/{BIO_DOI}v3.full.pdf" download>Main PDF</a><a href="{page}">Supplementary material</a>'
    assert strategy.supplementary_items(article_assets, page) == []

    # A small caption example using the observed HighWire row markup.
    html = (
        article_assets
        + """<div class="supplementary-material-expansion">
        <span class="supplementary-material-label">Supplementary Information</span>
        <a href="/content/medrxiv/early/DC1/embed/media-1.pdf">[supplements/supplement.pdf]</a>
        <p>Complete <em>unabridged</em> caption, including punctuation.</p>
    </div>"""
    )
    assert strategy.supplementary_items(html, page) == [
        SupplementaryItem(
            f"{MED}/content/medrxiv/early/DC1/embed/media-1.pdf",
            "Supplementary Information",
            "Complete unabridged caption, including punctuation.",
            "supplement.pdf",
        )
    ]


def test_science_supplementary_page_precedes_article() -> None:
    strategy = StrategyRegistry.get_for_doi(SCIENCE_DOI)
    assert strategy is not None
    assert strategy.supplementary_pages(SCIENCE_DOI) == [
        f"{SCIENCE}/doi/suppl/{SCIENCE_DOI}",
        f"{SCIENCE}/doi/{SCIENCE_DOI}",
    ]
    assert strategy.supplementary_items("<main>No supplements</main>", SCIENCE) == []


def test_science_article_supplementary_block() -> None:
    strategy = StrategyRegistry.get_for_doi(SCIENCE_DOI)
    assert strategy is not None
    html = (FIXTURES / "science-supplementary.html").read_text(encoding="utf-8")
    base = f"{SCIENCE}/doi/suppl/{SCIENCE_DOI}/suppl_file"
    files = [
        (
            "sm.pdf",
            "This PDF file includes:",
            "Materials and Methods\nFigs. S1 to S11\nTables S4, S5, S6, S10, S11, S12\nReferences (76–96)",
        ),
        (
            "mdar_reproducibility_checklist.pdf",
            "MDAR Reproducibility Checklist",
            "MDAR Reproducibility Checklist",
        ),
        (
            "supplementary_tables.zip",
            "Tables S1, S2, S3, S7, S8, S9, S13, S14, and S15",
            "Tables S1, S2, S3, S7, S8, S9, S13, S14, and S15",
        ),
        ("movie_s1.zip", "Movie S1", "Movie S1"),
    ]
    assert strategy.supplementary_items(html, f"{SCIENCE}/doi/{SCIENCE_DOI}") == [
        SupplementaryItem(
            f"{base}/science.adk4858_{suffix}",
            label,
            description,
            f"science.adk4858_{suffix}",
        )
        for suffix, label, description in files
    ]
