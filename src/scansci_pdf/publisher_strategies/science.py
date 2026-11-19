"""Science / AAAS publisher strategy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..science_transport import (
    _SCIENCE_DOI_PREFIX,
    _SCIENCE_EPDF_URL,
    _remember_local_science_cookies,
    _science_fetch_download,
    _wait_for_science_reader,
)
from ..supplementary import SupplementaryItem
from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class ScienceStrategy(BasePublisherStrategy):
    name = "Science"
    aliases = ("science", "aaas")
    doi_prefixes = (_SCIENCE_DOI_PREFIX,)
    base_domains = ("science.org", "sciencemag.org")
    sample_dois = ("10.1126/science.abc1234",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://www.science.org/doi/pdf/{doi}",
    )
    success_url_markers = ("science.org/doi/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)

    def supplementary_pages(self, doi: str) -> list[str]:
        science_doi = "https://www.science.org/doi"
        return [f"{science_doi}/suppl/{doi}", f"{science_doi}/{doi}"]

    def supplementary_items(self, html: str, page_url: str) -> list[SupplementaryItem]:
        from urllib.parse import unquote, urldefrag, urljoin, urlsplit

        from bs4 import BeautifulSoup


        soup = BeautifulSoup(html, "lxml")
        items: dict[str, SupplementaryItem] = {}
        for link in soup.select('a[href*="/suppl_file/"]'):
            url, _ = urldefrag(urljoin(page_url, link["href"]))
            target = urlsplit(url)
            filename = unquote(target.path.rsplit("/", 1)[-1])
            if target.scheme not in {"https", "http"} or not filename or "/suppl_file/" not in target.path:
                continue

            label = link.get_text().strip()
            description = ""
            row = link.find_parent(class_="core-supplementary-material")
            caption = row.select_one(".core-description") if row else None
            if caption:
                # The download link shows file type and size, not the item's title.
                # Read each caption block intact so inline references retain their punctuation.
                blocks = caption.find_all(["div", "p"], recursive=False)
                description = "\n".join(block.get_text().strip() for block in (blocks or [caption]))
                if label.lower().startswith("download") and description:
                    heading = row.parent.find("h3", recursive=False) if row.parent else None
                    label = heading.get_text().strip() if len(blocks) > 1 and heading else description.splitlines()[0]
            items.setdefault(url, SupplementaryItem(url, label, description, filename))
        return list(items.values())

    def browser_entry_url(self, doi: str, config: dict[str, Any]) -> str:
        from ..browser_backend import BACKEND_CDP, resolve_backend

        if resolve_backend(config) == BACKEND_CDP:
            return _SCIENCE_EPDF_URL.format(doi=doi)
        return self.article_url(doi)

    def prepare_download_page(
        self, tab_id: str, doi: str, html: str, config: dict[str, Any]
    ) -> tuple[str, list[str]]:
        from ..browser_engine import evaluate_js
        from .._publisher_strategies_core import _is_challenge_page

        timeout = config.get(
            "science_reader_timeout" if _is_challenge_page(html) else "science_reader_grace",
            60 if _is_challenge_page(html) else 5,
        )
        signed = _wait_for_science_reader(tab_id, config, timeout=float(timeout))
        if not signed:
            return html, []
        refreshed = evaluate_js(tab_id, "document.documentElement.outerHTML", config) or html
        _remember_local_science_cookies(config)
        return refreshed, [signed]

    async def download(
        self, doi: str, output_path: Path, config: dict[str, Any]
    ) -> dict[str, Any] | None:
        return self.download_sync(doi, output_path, config)

    def download_sync(
        self,
        doi: str,
        output_path: Path,
        config: dict[str, Any],
        *,
        tab_id: str | None = None,
    ) -> dict[str, Any] | None:
        from .._publisher_strategies_core import _browser_download_with_fallback
        from ..browser_backend import BACKEND_CDP, resolve_backend
        from ..pdf_utils import is_pdf_file, success

        if (
            resolve_backend(config) == BACKEND_CDP
            and doi.startswith(_SCIENCE_DOI_PREFIX)
            and _science_fetch_download(doi, output_path, config, tab_id=tab_id)
        ):
            return success(doi, output_path, "Science(Fetch)")
        entry_url = self.browser_entry_url(doi, config)
        if _browser_download_with_fallback(doi, entry_url, output_path, config, self.name):
            if is_pdf_file(output_path):
                return success(doi, output_path, "Science(Browser)")
        return None

    _uses_legacy_fn = False
