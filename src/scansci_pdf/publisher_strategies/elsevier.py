"""Elsevier / ScienceDirect / Cell Press publisher strategy."""

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlsplit

from ..sources.elsevier_api import ELSEVIER_API
from ..supplementary import SupplementaryItem
from .base import BasePublisherStrategy
from .registry import StrategyRegistry


_MMC_EID_RE = re.compile(r"1-s2\.0-[a-z0-9]+-(mmc\d+)\.[a-z0-9]+", re.I)


def _xml_text(element: ET.Element, name: str) -> str:
    node = element.find(f"{{*}}{name}")
    return "" if node is None else "".join(node.itertext()).strip()


def _api_supplementary_items(xml: str) -> list[SupplementaryItem] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    if root.tag.rsplit("}", 1)[-1] != "full-text-retrieval-response":
        return None

    metadata: dict[str, tuple[str, str]] = {}
    for component in root.findall(".//{*}e-component"):
        link = component.find("{*}link")
        if link is not None:
            locator = link.get("locator", "").lower()
            metadata.setdefault(locator, (_xml_text(component, "label"), _xml_text(component, "caption")))

    files: dict[str, dict[str, SupplementaryItem]] = {}
    for attachment in root.findall(".//{*}attachments/{*}attachment"):
        eid = _xml_text(attachment, "attachment-eid")
        match = _MMC_EID_RE.fullmatch(eid)
        kind = _xml_text(attachment, "attachment-type").upper()
        if not match or kind in {"IMAGE-MMC-DOWNSAMPLED", "IMAGE-MMC-THUMBNAIL"}:
            continue
        locator = match[1].lower()
        label, caption = metadata.get(locator, ("", ""))
        filename = _xml_text(attachment, "filename") or eid.rsplit("-", 1)[-1]
        files.setdefault(locator, {})[eid] = SupplementaryItem(
            f"{ELSEVIER_API}/object/eid/{eid}", label or locator, caption, filename
        )

    items = []
    for locator in dict.fromkeys([*metadata, *files]):
        variants = files.get(locator, {})
        # The same publisher movie can have both Flash and MP4 renditions.
        has_mp4 = any(eid.lower().endswith(".mp4") for eid in variants)
        items.extend(
            item for eid, item in variants.items()
            if not (has_mp4 and eid.lower().endswith(".flv"))
        )
    return items


@StrategyRegistry.register
class ElsevierStrategy(BasePublisherStrategy):
    def supplementary_pages(self, doi: str) -> list[str]:
        return [f"{ELSEVIER_API}/article/doi/{quote(doi, safe='/')}", self.article_url(doi)]

    def supplementary_items(self, html: str, page_url: str) -> list[SupplementaryItem] | None:
        if urlsplit(page_url).hostname != "api.elsevier.com":
            return None  # The article website uses generic discovery as a fallback.
        return _api_supplementary_items(html)
    name = "Elsevier"
    aliases = ("elsevier", "sciencedirect", "cellpress")
    doi_prefixes = (
        "10.1016/",
        "10.1016/j.",
        "10.1016/j.cell",
        "10.1016/j.oneear",
        "10.1016/j.cels",
        "10.1016/j.cub",
        "10.1016/j.neuron",
        "10.1016/j.molcel",
        "10.1016/j.devcel",
        "10.1016/j.immuni",
        "10.1016/j.chom",
        "10.1016/j.cmet",
        "10.1016/j.stem",
        "10.1016/j.celrep",
        "10.1016/j.isci",
        "10.1016/j.xcr",
        "10.1016/j.heliyon",
        "10.1016/j.ajhg",
    )
    base_domains = (
        "sciencedirect.com",
        "linkinghub.elsevier.com",
        "cell.com",
        "elsevier.com",
    )
    sample_dois = ("10.1016/j.cell.2022.01.001",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://www.sciencedirect.com/science/article/pii/{doi_suffix}/pdfft",
    )
    success_url_markers = (
        "sciencedirect.com/science/article/pii/",
        "cell.com/",
    )
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
        "shibboleth",
    )
    sso_text_markers = (
        "Access through your institution",
        "Institutional access",
        "Login via your institution",
    )
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = (
        "#institution-search",
        "input[name='query']",
        "#bdd-email",
    )
    institution_result_selectors = (
        "#institution-search",
        "input[name='query']",
    )
