"""Find PMC articles and their files in NCBI's PMC Cloud bucket.

NCBI publishes every PMC article as individual files under
``PMCID.VERSION/`` in the public ``pmc-oa-opendata`` S3 bucket. Open-access
articles carry the PDF, figures and supplementary files; author manuscripts
carry only the JATS XML, plain text and JSON metadata.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET

from requests import Response

IDCONV_URL = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
BUCKET_URL = "https://pmc-oa-opendata.s3.amazonaws.com"
BUCKET_HOST = "pmc-oa-opendata.s3.amazonaws.com"
EUROPEPMC_PDF_URL = "https://europepmc.org/api/getPdf"
_S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_PMCID = re.compile(r"PMC[0-9]+")
_OWN_SUFFIXES = (".xml", ".txt", ".json", ".pdf")

Fetch = Callable[[str], Response]


def lookup_pmcid(doi: str, fetch: Fetch) -> str | None:
    """Return the PMCID for a DOI, or None when PMC does not hold the article."""
    doi = doi.strip()
    query = urlencode({"ids": doi, "format": "json", "tool": "academic-source"})
    with fetch(f"{IDCONV_URL}?{query}") as response:
        response.raise_for_status()
        try:
            records = response.json().get("records")
        except (ValueError, AttributeError):
            return None
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("doi", "")).casefold() != doi.casefold():
            continue
        pmcid = record.get("pmcid")
        if isinstance(pmcid, str) and _PMCID.fullmatch(pmcid):
            return pmcid
    return None


@dataclass(frozen=True)
class CloudFile:
    key: str
    size: int

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    @property
    def url(self) -> str:
        return f"{BUCKET_URL}/{quote(self.key)}"


@dataclass(frozen=True)
class CloudArticle:
    pmcid: str
    version: int
    files: tuple[CloudFile, ...]

    def _own(self, suffix: str) -> CloudFile | None:
        name = f"{self.pmcid}.{self.version}{suffix}"
        return next((file for file in self.files if file.name == name), None)

    @property
    def xml(self) -> CloudFile | None:
        return self._own(".xml")

    @property
    def pdf(self) -> CloudFile | None:
        return self._own(".pdf")

    @property
    def attachments(self) -> tuple[CloudFile, ...]:
        """Figures and supplementary files: everything except the article's own renditions."""
        own = {f"{self.pmcid}.{self.version}{suffix}" for suffix in _OWN_SUFFIXES}
        return tuple(file for file in self.files if file.name not in own)


def find_article(pmcid: str, fetch: Fetch) -> CloudArticle | None:
    """List the latest version of an article in the bucket, with file sizes."""
    if not _PMCID.fullmatch(pmcid):
        raise ValueError(f"not a PMCID: {pmcid!r}")
    versions: dict[int, list[CloudFile]] = {}
    token: str | None = None
    while True:
        params = {"list-type": "2", "prefix": f"{pmcid}."}
        if token:
            params["continuation-token"] = token
        with fetch(f"{BUCKET_URL}/?{urlencode(params)}") as response:
            response.raise_for_status()
            listing = ET.fromstring(response.content)
        for entry in listing.iterfind(f"{_S3}Contents"):
            key = entry.findtext(f"{_S3}Key", "")
            folder, _, name = key.partition("/")
            stem, _, version = folder.rpartition(".")
            if stem != pmcid or not version.isdecimal() or not name:
                continue
            size = entry.findtext(f"{_S3}Size", "0")
            versions.setdefault(int(version), []).append(
                CloudFile(key, int(size) if size.isdecimal() else 0)
            )
        token = listing.findtext(f"{_S3}NextContinuationToken")
        if listing.findtext(f"{_S3}IsTruncated") != "true" or not token:
            break
    if not versions:
        return None
    latest = max(versions)
    return CloudArticle(pmcid, latest, tuple(versions[latest]))


def europepmc_pdf_url(pmcid: str) -> str:
    """Europe PMC's website renders PDFs for author manuscripts, which the bucket lacks."""
    return f"{EUROPEPMC_PDF_URL}?{urlencode({'pmcid': pmcid})}"
