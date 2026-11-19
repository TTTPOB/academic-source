from pathlib import Path

from scansci_pdf.publisher_strategies import StrategyRegistry
from scansci_pdf.supplementary import SupplementaryItem

FIXTURES = Path(__file__).parent / "fixtures" / "supplementary"
PLOS_DOI = "10.1371/journal.pgen.1011136"
PLOS_ARTICLE = "https://journals.plos.org/plosgenetics/article"


def test_plos_supplementary_pages():
    strategy = StrategyRegistry.get_for_doi(PLOS_DOI)
    assert strategy is not None
    assert strategy.supplementary_pages(PLOS_DOI) == [
        f"{PLOS_ARTICLE}/peerReview?id={PLOS_DOI}",
        f"{PLOS_ARTICLE}?id={PLOS_DOI}",
    ]


def test_plos_article_supplementary_items():
    strategy = StrategyRegistry.get_for_doi(PLOS_DOI)
    assert strategy is not None
    html = (FIXTURES / "plos-genetics.html").read_text(encoding="utf-8")
    assert strategy.supplementary_items(html, f"{PLOS_ARTICLE}?id={PLOS_DOI}") == [
        SupplementaryItem(
            url=f"{PLOS_ARTICLE}/file?type=supplementary&id={PLOS_DOI}.s001",
            label="S1 Fig.",
            description=(
                "Quantification of Indra overexpression.\n\n"
                "Indra amount in GSCs relative to CySCs under low rDNA condition without or with "
                "Indra overexpression in GSCs. Because CySCs do not express Indra transgene, it "
                "serves as an appropriate denominator to determine the level of Indra overexpression "
                "in GSCs. n = number of GSCs scored. P value, two-tailed Mann-Whitney test. The "
                "error bar indicates the median with a 95% CI."
            ),
        ),
        SupplementaryItem(
            url=f"{PLOS_ARTICLE}/file?type=supplementary&id={PLOS_DOI}.s005",
            label="S1 Table.",
            description="Probe sequences for RNA FISH, DNA FISH, and CO-FISH.",
        ),
        SupplementaryItem(
            url=f"{PLOS_ARTICLE}/file?type=supplementary&id={PLOS_DOI}.s006",
            label="S1 Raw Data.",
            description="Containing raw data used to generate graphs presented in the manuscript.",
        ),
    ]


def test_plos_peer_review_attachments():
    strategy = StrategyRegistry.get_for_doi(PLOS_DOI)
    assert strategy is not None
    html = (FIXTURES / "plos-genetics-peer-review.html").read_text(encoding="utf-8")
    assert strategy.supplementary_items(
        html, f"{PLOS_ARTICLE}/peerReview?id={PLOS_DOI}"
    ) == [
        SupplementaryItem(
            url=f"{PLOS_ARTICLE}/file?id={PLOS_DOI}.s007&type=supplementary",
            label="Attachment",
            description="Submitted filename: Inrda-response 420.docx",
            filename="Inrda-response 420.docx",
        ),
    ]


def test_plos_without_supplementary_material():
    strategy = StrategyRegistry.get_for_doi(PLOS_DOI)
    assert strategy is not None
    html = f'<a href="{PLOS_ARTICLE}/file?id={PLOS_DOI}&amp;type=printable">Download PDF</a>'
    assert strategy.supplementary_items(html, f"{PLOS_ARTICLE}?id={PLOS_DOI}") == []


def test_oxford_supplementary_items_keep_signed_urls():
    strategy = StrategyRegistry.get_for_doi("10.1093/nar/gkt922")
    assert strategy is not None
    cdn = "https://oup.silverchair-cdn.com/oup/backfile/Content_public/Journal/nar/42/2"
    html = f"""
    <a class="download-slide" href="/DownloadFile/DownloadImage.aspx?image=x">Download slide</a>
    <div class="widget-ArticleDataSupplements">
      <h2 id="supplementary-data">Supplementary data</h2>
      <div class="dataSuppLink"><a href="{cdn}/gkt922-File006.pdf?Expires=1&amp;Signature=s"
        rel="nofollow">AddSuppFiles-1</a> - pdf file</div>
      <div class="dataSuppLink"><a href="/nar/suppl/table%20s2.xlsx">Table S2</a> - xlsx file</div>
    </div>
    """
    page = "https://academic.oup.com/nar/article/42/2/1341/1025550"
    assert strategy.supplementary_items(html, page) == [
        SupplementaryItem(
            url=f"{cdn}/gkt922-File006.pdf?Expires=1&Signature=s",
            label="AddSuppFiles-1",
            description="pdf file",
            filename="gkt922-File006.pdf",
        ),
        SupplementaryItem(
            url="https://academic.oup.com/nar/suppl/table%20s2.xlsx",
            label="Table S2",
            description="xlsx file",
            filename="table s2.xlsx",
        ),
    ]
    assert strategy.supplementary_items("<p>No supplements</p>", page) == []
