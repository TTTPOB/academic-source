"""PLOS supporting information and peer-review attachments."""

from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..publisher_pdf_router import PLOS_DOI_JOURNAL_RE, PLOS_JOURNAL_PATHS
from ..supplementary import SupplementaryItem
from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class PlosStrategy(BasePublisherStrategy):
    name = "PLOS"
    doi_prefixes = ("10.1371/",)

    def article_url(self, doi: str) -> str:
        doi = doi.strip()
        match = PLOS_DOI_JOURNAL_RE.match(doi)
        journal = PLOS_JOURNAL_PATHS.get(match.group(1).lower()) if match else None
        if journal is None:
            return super().article_url(doi)
        return f"https://journals.plos.org/{journal}/article?id={doi}"

    def supplementary_pages(self, doi: str) -> list[str]:
        article_url = self.article_url(doi)
        parts = urlsplit(article_url)
        if parts.hostname != "journals.plos.org":
            return [article_url]
        peer_review_url = urlunsplit(parts._replace(path=f"{parts.path}/peerReview"))
        return [peer_review_url, article_url]

    def supplementary_items(self, html: str, page_url: str) -> list[SupplementaryItem]:
        soup = BeautifulSoup(html, "html.parser")
        items = []
        for block in soup.select(".supplementary-material"):
            for link in block.select(".siTitle a[href], a.supplementary-material__label[href]"):
                url = urljoin(page_url, str(link["href"]))
                parts = urlsplit(url)
                query = parse_qs(parts.query)
                if not parts.path.endswith("/article/file") or query.get("type") != ["supplementary"]:
                    continue

                label = link.get_text().strip()
                description = []
                title = block.select_one(".siTitle")
                if title is not None:
                    title_text = title.get_text().strip().removeprefix(label).strip()
                    if title_text:
                        description.append(title_text)
                for caption in block.select(".preSiDOI, .supplementary-material__caption"):
                    if text := caption.get_text().strip():
                        description.append(text)

                # The DOI in the query and the (TIF) marker are not filenames.
                filename = str(link.get("download", "")).strip()
                submitted = block.select_one(".supplementary-material__caption i")
                if not filename and submitted is not None:
                    filename = submitted.get_text().strip()
                items.append(SupplementaryItem(url, label, "\n\n".join(description), filename))
        return items
