"""Retrieve supplementary files and build optional exports."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from academic_source.domain import AcquisitionRequest, AcquisitionResult, Artifact
from academic_source.infrastructure.storage import Store
from academic_source.sources.lanes import ProviderLanes

log = logging.getLogger(__name__)


def add_exports(
    result: AcquisitionResult,
    pdf: Path,
    request: AcquisitionRequest,
    work: Path,
    config: dict[str, Any],
    store: Store,
    cached_kinds: set[str],
) -> dict[str, list[str]]:
    successful: dict[str, list[str]] = {}
    identifier = result.identifier
    if request.markdown and "markdown" not in cached_kinds:
        warnings = add_markdown(
            result, result.artifacts[0], pdf, work / "document.md", store
        )
        if warnings is not None:
            successful["markdown"] = warnings
    if request.bibtex and "bibtex" not in cached_kinds:
        if not identifier.startswith("10."):
            result.warnings.append("BibTeX unavailable for this identifier")
        else:
            try:
                from scansci_pdf.bibtex import fetch_bibtex

                text = fetch_bibtex(identifier, config)
                if text:
                    path = work / "citation.bib"
                    path.write_text(text, encoding="utf-8", newline="\n")
                    result.artifacts.append(
                        store.import_artifact(
                            path,
                            kind="bibtex",
                            identifier=identifier,
                            source="Crossref",
                        )
                    )
                    successful["bibtex"] = []
                else:
                    result.warnings.append("BibTeX unavailable")
            except Exception:
                log.exception("Citation export failed for %s", identifier)
                result.warnings.append("BibTeX unavailable; see server logs")
    return successful


def add_markdown(
    result: AcquisitionResult, parent: Artifact, pdf: Path, path: Path, store: Store
) -> list[str] | None:
    supplementary = parent.kind == "supplementary"
    try:
        from scansci_pdf.md_export import pdf_to_markdown_detailed

        text, warnings = pdf_to_markdown_detailed(pdf, write=False)
        path.write_text(str(text), encoding="utf-8", newline="\n")
        result.artifacts.append(
            store.import_artifact(
                path,
                kind="markdown",
                identifier=result.identifier,
                source=parent.provenance.source,
                derived_from=parent.id,
                note=f"Converted from supplementary file: {parent.filename}"
                if supplementary
                else "",
            )
        )
    except Exception as exc:  # noqa: BLE001 - Keep the paper and omit unchecked exception text.
        log.error(
            "Markdown export failed for %s (%s)", result.identifier, type(exc).__name__
        )
        subject = f" for supplementary file {parent.filename}" if supplementary else ""
        result.warnings.append(f"Markdown unavailable{subject}; see server logs")
        return None
    if supplementary:
        warnings = [f"{parent.filename}: {warning}" for warning in warnings]
    result.warnings.extend(warnings)
    return warnings


@dataclass(frozen=True)
class SupplementaryExport:
    failure: str = ""
    cacheable: bool = False


def add_supplementary(
    result: AcquisitionResult,
    work: Path,
    config: dict[str, Any],
    store: Store,
    *,
    lanes: ProviderLanes | None = None,
) -> SupplementaryExport:
    from scansci_pdf.supplementary import UnsavedSupplementary, fetch_supplementary

    identifier = result.identifier
    if not identifier.startswith("10."):
        result.warnings.append("Supplementary material requires a DOI")
        return SupplementaryExport(failure="unsupported")
    try:
        fetched = fetch_supplementary(
            identifier,
            work / "supplementary",
            config,
            run_browser=lanes.browser if lanes is not None else None,
        )
    except Exception as exc:  # noqa: BLE001 - Keep the paper and omit unchecked exception text.
        log.error(
            "Supplementary retrieval failed for %s (%s)", identifier, type(exc).__name__
        )
        result.warnings.append("Supplementary material unavailable; see server logs")
        return SupplementaryExport(failure="supplementary_unavailable")
    result.warnings.extend(fetched.route_warnings)
    if not fetched.pages_read:
        result.warnings.append("Could not read any supplementary material pages")
    elif fetched.pages_unread:
        result.warnings.append(
            f"Could not read {fetched.pages_unread} supplementary material page(s)"
        )
    found = len(fetched.saved) + len(fetched.unsaved)
    if not found:
        if not fetched.pages_read:
            return SupplementaryExport(failure="supplementary_page_unavailable")
        result.warnings.append("No supplementary material found (0 found, 0 saved)")
        return SupplementaryExport()

    unsaved = list(fetched.unsaved)
    saved_count = 0
    for saved in fetched.saved:
        item = saved.item
        page_url = _public_url(saved.page_url)
        note = "\n".join(
            part
            for part in (
                item.label,
                item.description,
                f"Original file: {item.filename}" if item.filename else "",
                f"Source page: {page_url}" if page_url else "",
                f"Obtained from Europe PMC ({fetched.pmcid})" if fetched.pmcid else "",
            )
            if part
        )
        try:
            artifact = store.import_artifact(
                saved.path,
                kind="supplementary",
                identifier=identifier,
                source=fetched.source,
                url=_public_url(item.url) or None,
                note=note,
            )
        except Exception as exc:  # noqa: BLE001 - Keep the paper and omit unchecked exception text.
            log.error(
                "Supplementary import failed for %s (%s)",
                identifier,
                type(exc).__name__,
            )
            unsaved.append(UnsavedSupplementary(item, "could not store file"))
        else:
            result.artifacts.append(artifact)
            saved_count += 1
    result.warnings.append(
        f"Supplementary material via {fetched.route}: {found} found, {saved_count} saved"
    )
    for skipped in unsaved:
        label = (
            " ".join((skipped.item.label or skipped.item.filename).split())
            or "Unlabeled file"
        )
        url = _public_url(skipped.item.url) or "URL unavailable"
        result.warnings.append(
            f"Supplementary material not saved: {label} ({url}): {skipped.reason}"
        )
    return SupplementaryExport(
        failure="" if saved_count else "supplementary_download_failed",
        cacheable=not unsaved and not fetched.pages_unread,
    )


def _public_url(url: str) -> str:
    # Query strings and fragments can contain credentials or signed download tokens.
    parsed = urlsplit(url)
    return parsed._replace(
        netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""
    ).geturl()
