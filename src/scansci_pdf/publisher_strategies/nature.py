"""Nature / Nature Portfolio publisher strategy."""

import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..supplementary import SupplementaryItem
from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class NatureStrategy(BasePublisherStrategy):
    def supplementary_pages(self, doi: str) -> list[str]:
        # The file list is embedded in the article, not on a separate page.
        return [self.article_url(doi)]

    def supplementary_items(self, html: str, page_url: str) -> list[SupplementaryItem]:
        soup = BeautifulSoup(html, "html.parser")
        items = []
        for item in soup.select(".c-article-supplementary__item"):
            title = item.select_one(".c-article-supplementary__title")
            links = title.select("a[href]") if title else item.select("a[href]")
            for link in links:
                url = urljoin(page_url, link["href"])
                parts = urlsplit(url)
                filename = unquote(parts.path.rsplit("/", 1)[-1])
                if parts.scheme not in {"http", "https"} or not re.search(
                    r"(?:^|_)MOESM\d+_ESM\.[a-z0-9]+$", filename, re.I
                ):
                    continue
                label = re.sub(
                    r"\s*\(download\s+[^()]+\)\s*$", "", link.get_text().strip(), flags=re.I
                )
                caption = "\n".join(p.get_text().strip() for p in item.select("p"))
                items.append(SupplementaryItem(url, label, caption, filename))
        return items

    name = "Nature"
    aliases = ("nature",)
    doi_prefixes = ("10.1038/",)
    base_domains = ("nature.com",)
    sample_dois = ("10.1038/s41586-020-2649-2",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://www.nature.com/articles/{doi_suffix}.pdf",
    )
    success_url_markers = ("nature.com/articles/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='idpSearch']", "#searchInstitution")
    institution_result_selectors = ("input[name='idpSearch']",)
