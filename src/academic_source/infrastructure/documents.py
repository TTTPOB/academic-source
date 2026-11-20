"""Validate the file we are about to expose, without judging article length."""

import re
from pathlib import Path
from threading import Lock

_PDF_READER = Lock()
# Marks preprint servers stamp on their copies' first page.
_PREPRINT_STAMPS = re.compile(
    # bioRxiv / medRxiv header; text extraction can split or reorder its lines.
    r"\b(?:bioRxiv|medRxiv)\s+preprint\s+doi\b"
    r"|The\s+copyright\s+holder\s+for\s+this\s+preprint"
    r"|\barXiv:\d{4}\.\d{4,5}v\d+\s*\[[\w.-]+\]"  # arXiv margin: arXiv:1710.02563v3 [q-bio.NC]
    r"|doi\.org/10\.21203/rs\."  # Research Square
    r"|Electronic copy available at: https?://ssrn\.com/abstract="  # SSRN
    r"|10\.20944/preprints",  # Preprints.org
    re.IGNORECASE,
)


def is_readable_pdf(path: Path) -> bool:
    import pymupdf

    try:
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return False
        # PyMuPDF is not thread-safe; HTTP sources may finish simultaneously.
        with _PDF_READER, pymupdf.open(path) as document:
            return (
                document.is_pdf and document.page_count > 0 and not document.needs_pass
            )
    except (OSError, RuntimeError, ValueError):
        return False


def looks_like_preprint(path: Path) -> bool:
    """True when the first page carries a preprint server's stamp."""
    import pymupdf

    try:
        with _PDF_READER, pymupdf.open(path) as document:
            text = document[0].get_text() if document.page_count else ""
    except (OSError, RuntimeError, ValueError, IndexError):
        return False
    return bool(_PREPRINT_STAMPS.search(text))
