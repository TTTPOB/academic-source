"""Oxford Academic publisher strategy."""

from posixpath import basename
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..supplementary import SupplementaryItem
from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class OxfordStrategy(BasePublisherStrategy):
    name = "Oxford"
    aliases = ("oxford", "oup")
    doi_prefixes = ("10.1093/",)
    base_domains = ("academic.oup.com", "oup.com")
    sample_dois = ("10.1093/bioinformatics/btz076",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://academic.oup.com/downloadpdf/{doi}",
    )
    success_url_markers = ("academic.oup.com/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("#searchInstitution", "input[name='query']")
    institution_result_selectors = ("#searchInstitution",)

    def supplementary_items(self, html: str, page_url: str) -> list[SupplementaryItem]:
        soup = BeautifulSoup(html, "html.parser")
        items = []
        for holder in soup.select(".dataSuppLink"):
            link = holder.select_one("a[href]")
            if link is None:
                continue
            # The links are signed; the query string must reach the download intact.
            url = urljoin(page_url, str(link["href"]))
            label = link.get_text().strip()
            # The text after the link names the file type, e.g. "- pdf file".
            description = holder.get_text().strip().removeprefix(label).strip(" -\n")
            filename = unquote(basename(urlsplit(url).path))
            items.append(SupplementaryItem(url, label, description, filename))
        return items
