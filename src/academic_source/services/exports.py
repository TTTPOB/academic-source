"""Optional exports never change whether the requested PDF was acquired."""

import logging
from pathlib import Path
from typing import Any

from academic_source.domain import AcquisitionRequest, AcquisitionResult
from academic_source.infrastructure.storage import Store

log = logging.getLogger(__name__)


def add_exports(
    result: AcquisitionResult,
    pdf: Path,
    request: AcquisitionRequest,
    work: Path,
    config: dict[str, Any],
    store: Store,
) -> None:
    identifier = result.identifier
    source = result.artifacts[0].provenance.source
    if request.markdown:
        try:
            from scansci_pdf.md_export import pdf_to_markdown_detailed

            text, warnings = pdf_to_markdown_detailed(pdf, write=False)
            result.warnings.extend(warnings)
            path = work / "document.md"
            path.write_text(str(text), encoding="utf-8")
            result.artifacts.append(
                store.import_artifact(
                    path,
                    kind="markdown",
                    identifier=identifier,
                    source=source,
                    derived_from=result.artifacts[0].id,
                )
            )
        except Exception:
            log.exception("Markdown export failed for %s", identifier)
            result.warnings.append("Markdown unavailable; see server logs")
    if request.bibtex:
        if not identifier.startswith("10."):
            result.warnings.append("BibTeX unavailable for this identifier")
        else:
            try:
                from scansci_pdf.bibtex import fetch_bibtex

                text = fetch_bibtex(identifier, config)
                if text:
                    path = work / "citation.bib"
                    path.write_text(text, encoding="utf-8")
                    result.artifacts.append(
                        store.import_artifact(
                            path,
                            kind="bibtex",
                            identifier=identifier,
                            source="Crossref",
                        )
                    )
                else:
                    result.warnings.append("BibTeX unavailable")
            except Exception:
                log.exception("Citation export failed for %s", identifier)
                result.warnings.append("BibTeX unavailable; see server logs")
    if request.supplementary:
        if not identifier.startswith("10."):
            result.warnings.append(
                "Supplementary material unavailable for this identifier"
            )
        else:
            try:
                from scansci_pdf.supplementary import fetch_supplementary

                files = fetch_supplementary(identifier, work / "supplementary", config)
                for filename in files:
                    result.artifacts.append(
                        store.import_artifact(
                            Path(filename),
                            kind="supplementary",
                            identifier=identifier,
                            source=source,
                        )
                    )
                if not files:
                    result.warnings.append("No supplementary material retrieved")
            except Exception:
                log.exception("Supplementary retrieval failed for %s", identifier)
                result.warnings.append("Supplementary unavailable; see server logs")
