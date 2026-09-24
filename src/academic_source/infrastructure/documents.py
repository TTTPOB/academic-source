"""Validate the file we are about to expose, without judging article length."""

from pathlib import Path
from threading import Lock

_PDF_READER = Lock()


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
