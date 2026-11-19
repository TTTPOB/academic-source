"""Supplementary discovery for bioRxiv and medRxiv (HighWire)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..supplementary import SupplementaryItem
from .generic import GenericStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class BioRxivStrategy(GenericStrategy):
    # Keep the name used by the main-download source plan.
    name = "bioRxiv"
    aliases = ("medRxiv",)
    doi_prefixes = ("10.1101/",)

    def article_url(self, doi: str) -> str:
        return f"https://www.biorxiv.org/content/{doi}"

    def supplementary_pages(self, doi: str) -> list[str]:
        article = self.article_url(doi)
        # The two servers share a DOI prefix and their suffixes overlap.
        medical = article.replace("www.biorxiv.org", "www.medrxiv.org", 1)
        return [f"{article}.supplementary-material", article, f"{medical}.supplementary-material", medical]

    def supplementary_items(self, html: str, page_url: str) -> list[SupplementaryItem]:
        soup = BeautifulSoup(html, "lxml")
        items: dict[str, SupplementaryItem] = {}
        for link in soup.select("a[href]"):
            url, _ = urldefrag(urljoin(page_url, link["href"]))
            target = urlsplit(url)
            if target.scheme not in {"http", "https"}:
                continue
            path = target.path
            filename = unquote(PurePosixPath(path).name)
            is_media = re.search(r"/DC\d+/(?:embed/)?media-\d+\.[^/]+$", path, re.I)
            is_adjunct = "/field_highwire_adjunct_files/" in path
            is_review = "/highwire/filestream/" in path and "review" in path.lower()
            if not (is_media or is_adjunct or is_review):
                continue
            row = link.find_parent(class_="supplementary-material-expansion") or link.find_parent("li")
            label_node = row.select_one(".supplementary-material-label") if row else None
            link_text = link.get_text().strip()
            label = label_node.get_text().strip() if label_node else link_text
            shown_name = link_text.strip("[]")
            if link.get("download"):
                filename = link["download"]
            elif is_media and PurePosixPath(shown_name).suffix == PurePosixPath(filename).suffix:
                filename = PurePosixPath(shown_name).name

            description = ""
            if row:
                # Read the text around the link without repeating the title or filename.
                description = "".join(
                    str(text)
                    for text in row.strings
                    if link not in text.parents and (label_node is None or label_node not in text.parents)
                ).strip()
            items.setdefault(url, SupplementaryItem(url, label, description, filename))
        return list(items.values())
