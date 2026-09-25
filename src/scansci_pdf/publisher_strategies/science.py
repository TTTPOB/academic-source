"""Science / AAAS publisher strategy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..science_transport import (
    _SCIENCE_DOI_PREFIX,
    _SCIENCE_EPDF_URL,
    _capture_science_clearance,
    _science_http_download,
    _wait_for_science_reader,
)
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
        _capture_science_clearance(tab_id, config)
        return refreshed, [signed]

    async def download(
        self, doi: str, output_path: Path, config: dict[str, Any]
    ) -> dict[str, Any] | None:
        return self.download_sync(doi, output_path, config)

    def download_sync(
        self, doi: str, output_path: Path, config: dict[str, Any]
    ) -> dict[str, Any] | None:
        from ..browser_cookies import load_science_http_state
        from ..pdf_utils import is_pdf_file, success
        from .._publisher_strategies_core import _browser_download_with_fallback

        state = load_science_http_state(config) if doi.startswith(_SCIENCE_DOI_PREFIX) else None
        if state and _science_http_download(doi, output_path, config, state):
            if is_pdf_file(output_path):
                return success(doi, output_path, "Science(Signed)")
        entry_url = self.browser_entry_url(doi, config)
        if _browser_download_with_fallback(doi, entry_url, output_path, config, self.name):
            if is_pdf_file(output_path):
                return success(doi, output_path, "Science(Browser)")
        return None

    _uses_legacy_fn = False
