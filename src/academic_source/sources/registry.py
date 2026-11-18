"""Provider inventory and policy ordering for the acquisition fallback chain."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, cast

from ..domain import AcquisitionRequest, Policy
from .legacy import LegacyHandler

Handler = tuple[str, LegacyHandler]
Category = Literal["oa", "publisher", "grey", "institutional"]
Enabled = Callable[[str, dict[str, Any]], bool]
FAST_POLICIES = {"fastest", "oa_first", "legal_only"}


@dataclass(frozen=True)
class ProviderSpec:
    category: Category
    site_key: str
    handler: str | LegacyHandler | None = None
    enabled: Enabled | None = None
    ownership: Literal["owned", "borrowed"] = "borrowed"
    # Do not mark a borrowed handler as HTTP until its call paths are audited.
    backend: Literal["http", "browser"] = "browser"
    # These labels also have a native HTTP implementation in the OA fast path.
    fast_path_backend: Literal["http"] | None = None

    def is_enabled(self, doi: str, config: dict[str, Any]) -> bool:
        return self.enabled is None or self.enabled(doi, config)

    def resolve(self) -> LegacyHandler:
        if isinstance(self.handler, str):
            module, attr = self.handler.split(":")
            return cast(
                LegacyHandler,
                getattr(import_module(f"scansci_pdf.sources.{module}"), attr),
            )
        assert self.handler is not None
        return self.handler


def _broker_source(
    doi: str, path: Path, config: dict[str, Any]
) -> dict[str, Any] | None:
    from scansci_pdf.institutional.instsci_bridge import _try_session_broker
    from scansci_pdf.institutional.publisher_profiles import infer_publisher_profile

    return _try_session_broker(doi, path, config, infer_publisher_profile(doi))


def _publisher_batch_source(
    doi: str, path: Path, config: dict[str, Any]
) -> dict[str, Any] | None:
    from scansci_pdf.institutional.instsci_bridge import _try_browser_batch_download
    from scansci_pdf.institutional.publisher_profiles import infer_publisher_profile

    return _try_browser_batch_download(doi, path, config, infer_publisher_profile(doi))


def _sage_cn_source(
    doi: str, path: Path, config: dict[str, Any]
) -> dict[str, Any] | None:
    from scansci_pdf.sources.sage_cn import try_sage_cn_authorized

    if try_sage_cn_authorized(doi, path, config):
        return {"success": True, "file": str(path), "source": "SAGECN"}
    return None


def _arxiv_source(
    reference: str, path: Path, config: dict[str, Any]
) -> dict[str, Any] | None:
    from scansci_pdf.sources.arxiv import download_arxiv_pdf

    return download_arxiv_pdf(
        f"https://arxiv.org/pdf/{reference.removeprefix('arxiv:')}.pdf", path, config
    )


def _grey_enabled(_doi: str, config: dict[str, Any]) -> bool:
    return bool(config.get("scihub_enabled", True))


def _carsi_enabled(_doi: str, config: dict[str, Any]) -> bool:
    return bool(config.get("carsi_enabled") and config.get("carsi_idp_name"))


def _institution_enabled(doi: str, config: dict[str, Any]) -> bool:
    return bool(
        _carsi_enabled(doi, config)
        or config.get("vpnsci_enabled")
        or config.get("ezproxy_enabled")
    )


# Insertion order is the order within the OA, grey, and institutional groups.
# Publisher order is specific to a DOI and comes from the retained publisher table.
PROVIDERS: dict[str, ProviderSpec] = {
    "Unpaywall": ProviderSpec(
        "oa",
        "unpaywall.org",
        "unpaywall:try_unpaywall",
        backend="http",
        fast_path_backend="http",
    ),
    "OpenAlexOA": ProviderSpec(
        "oa",
        "openalex.org",
        "openalex:try_openalex_oa",
        backend="http",
        fast_path_backend="http",
    ),
    "OpenAIRE": ProviderSpec("oa", "openaire.eu", "openaire:try_openaire"),
    "DOAJ": ProviderSpec("oa", "doaj.org", "oa_discovery:try_doaj"),
    "CrossrefPage": ProviderSpec("oa", "crossref", "crossref:try_crossref_page_scrape"),
    "EuropePMC": ProviderSpec(
        "oa",
        "europepmc.org",
        "europepmc:try_europepmc",
        backend="http",
        fast_path_backend="http",
    ),
    "CORE": ProviderSpec("oa", "core.ac.uk", "core_api:try_core", backend="http"),
    "PMC": ProviderSpec(
        "oa",
        "ncbi.nlm.nih.gov",
        "europepmc:try_pmc",
        backend="http",
        fast_path_backend="http",
    ),
    "OpenAlexContent": ProviderSpec(
        "oa",
        "openalex.org",
        "openalex:try_openalex_content_api",
        lambda _doi, config: bool(config.get("openalex_api_key")),
        backend="http",
        fast_path_backend="http",
    ),
    "OACDP": ProviderSpec(
        "oa",
        "candidate-origin",
        ownership="owned",
        enabled=lambda _doi, config: (
            str(config.get("browser_backend", "")).lower() == "cdp"
        ),
    ),
    "Crossref": ProviderSpec("publisher", "crossref", backend="http"),
    "NatureDirect": ProviderSpec("publisher", "nature.com", backend="http"),
    "PublisherDirect": ProviderSpec("publisher", "publisher-origin"),
    "NatureBrowser": ProviderSpec("publisher", "nature.com"),
    "RoyalSocietyBrowser": ProviderSpec("publisher", "royalsocietypublishing.org"),
    "CopernicusDirect": ProviderSpec("publisher", "copernicus.org"),
    "MDPIDirect": ProviderSpec("publisher", "mdpi.com"),
    "ElsevierAPI": ProviderSpec("publisher", "api.elsevier.com"),
    "ElsevierBrowser": ProviderSpec("publisher", "sciencedirect.com"),
    "WileyBrowser": ProviderSpec("publisher", "wiley.com"),
    "ScienceDirect": ProviderSpec("publisher", "science.org"),
    "ScienceBrowser": ProviderSpec("publisher", "science.org"),
    "PNASDirect": ProviderSpec("publisher", "pnas.org"),
    "OxfordBrowser": ProviderSpec("publisher", "academic.oup.com"),
    "ACSBrowser": ProviderSpec("publisher", "pubs.acs.org"),
    "SpringerTDM": ProviderSpec("publisher", "springernature.com"),
    "SpringerBrowser": ProviderSpec("publisher", "springer.com"),
    "APSBrowser": ProviderSpec("publisher", "journals.aps.org"),
    "IOPBrowser": ProviderSpec("publisher", "iopscience.iop.org"),
    "PLOSDirect": ProviderSpec("publisher", "journals.plos.org"),
    "FrontiersDirect": ProviderSpec("publisher", "frontiersin.org"),
    "BMCDirect": ProviderSpec("publisher", "link.springer.com"),
    "RSCBrowser": ProviderSpec("publisher", "pubs.rsc.org"),
    "AIPBrowser": ProviderSpec("publisher", "pubs.aip.org"),
    "ACMBrowser": ProviderSpec("publisher", "dl.acm.org"),
    "IEEEBrowser": ProviderSpec("publisher", "ieeexplore.ieee.org"),
    "TandFBrowser": ProviderSpec("publisher", "tandfonline.com"),
    "SAGEBrowser": ProviderSpec("publisher", "journals.sagepub.com"),
    "ASCEBrowser": ProviderSpec("publisher", "ascelibrary.org"),
    "arXiv": ProviderSpec("publisher", "arxiv.org", _arxiv_source, backend="http"),
    "SciBban": ProviderSpec(
        "grey", "scibban", "scibban:try_scibban", _grey_enabled, backend="http"
    ),
    "LibGen": ProviderSpec("grey", "libgen", "libgen:try_libgen", _grey_enabled),
    "Sci-Hub": ProviderSpec("grey", "scihub", "scihub:try_scihub", _grey_enabled),
    "SAGECN": ProviderSpec(
        "institutional",
        "sage-cn",
        _sage_cn_source,
        lambda doi, config: doi.startswith("10.1177/") and _carsi_enabled(doi, config),
    ),
    "CARSI": ProviderSpec(
        "institutional", "carsi", "carsi_source:try_carsi", _carsi_enabled
    ),
    "WebVPN": ProviderSpec(
        "institutional",
        "webvpn",
        "vpnsci:try_vpnsci",
        lambda _doi, config: bool(config.get("vpnsci_enabled")),
    ),
    "EZProxy": ProviderSpec(
        "institutional",
        "ezproxy",
        "ezproxy:try_ezproxy",
        lambda _doi, config: bool(config.get("ezproxy_enabled")),
    ),
    "SessionBroker": ProviderSpec(
        "institutional", "session-broker", _broker_source, _institution_enabled
    ),
    "InstitutionalBrowser": ProviderSpec(
        "institutional",
        "publisher-origin",
        _publisher_batch_source,
        _institution_enabled,
    ),
}

POLICY_ORDER: dict[Policy, tuple[Category, ...]] = {
    "fastest": ("publisher", "oa", "grey", "institutional"),
    "oa_first": ("oa", "publisher", "grey", "institutional"),
    "legal_only": ("publisher", "oa", "institutional"),
    "scihub_first": ("grey", "publisher", "oa", "institutional"),
    "grey_only": ("grey",),
    "scihub_only": ("grey",),
}


def _publisher_handlers(
    doi: str, policy: Policy, config: dict[str, Any]
) -> list[Handler]:
    from scansci_pdf.sources.publishers import get_publisher_fast_sources

    handlers = [
        (label, handler)
        for handler, label in get_publisher_fast_sources(doi)
        if not (
            policy in FAST_POLICIES and PROVIDERS[label].fast_path_backend is not None
        )
    ]
    if (
        doi.startswith("10.1016/")
        and policy in ("fastest", "legal_only")
        and (config.get("elsevier_api_key") or os.environ.get("ELSEVIER_API_KEY"))
    ):
        handlers.sort(key=lambda item: item[0] != "ElsevierAPI")
    return handlers


def plan(
    doi: str, request: AcquisitionRequest, config: dict[str, Any], skip_oa: bool = False
) -> list[Handler]:
    policy = request.policy
    ordered: dict[str, LegacyHandler] = {}
    for category in POLICY_ORDER[policy]:
        if category == "publisher":
            for label, handler in _publisher_handlers(doi, policy, config):
                ordered.setdefault(label, handler)
            continue
        labels = [
            label
            for label, spec in PROVIDERS.items()
            if spec.category == category
            and spec.handler is not None
            and not (policy in FAST_POLICIES and spec.fast_path_backend is not None)
            and not (policy == "scihub_only" and label != "Sci-Hub")
            and spec.is_enabled(doi, config)
        ]
        if category == "oa" and skip_oa:
            logging.getLogger("academic_source.sources").info(
                "skip identifier=%s reason=closed_access sources=%s",
                doi,
                ",".join(labels),
            )
            continue
        for label in labels:
            if label not in ordered:
                ordered[label] = PROVIDERS[label].resolve()
    return list(ordered.items())
