"""Thin adapters for existing source functions, without the old download orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..domain import AcquisitionRequest


class Source(Protocol):
    def supports(self, identifier: str, request: AcquisitionRequest) -> bool: ...

    def acquire(
        self,
        identifier: str,
        request: AcquisitionRequest,
        work_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any] | None: ...


class LegacySources:
    """Run individual legacy site handlers; never call legacy download/batch_download."""

    def supports(self, identifier: str, request: AcquisitionRequest) -> bool:
        return identifier.startswith("10.") or bool(self._arxiv(identifier))

    @staticmethod
    def _arxiv(identifier: str) -> str | None:
        from scansci_pdf.identifiers import normalize_arxiv_id

        return normalize_arxiv_id(identifier)

    def acquire(
        self,
        identifier: str,
        request: AcquisitionRequest,
        work_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        from scansci_pdf.sources.arxiv import try_arxiv
        from scansci_pdf.sources.europepmc import try_europepmc, try_pmc
        from scansci_pdf.sources.oa_discovery import try_doaj
        from scansci_pdf.sources.openalex import try_openalex_oa
        from scansci_pdf.sources.publishers import get_publisher_fast_sources
        from scansci_pdf.sources.unpaywall import try_unpaywall

        attempts: list[dict[str, str]] = []
        if self._arxiv(identifier):
            handlers = [("arXiv", try_arxiv)]
        else:
            publisher = [
                (label, fn) for fn, label in get_publisher_fast_sources(identifier)
            ]
            # Preserve publisher ordering, then fill out OA sources without repeating handlers.
            oa = [
                ("Unpaywall", try_unpaywall),
                ("OpenAlexOA", try_openalex_oa),
                ("PMC", try_pmc),
                ("EuropePMC", try_europepmc),
                ("DOAJ", try_doaj),
            ]
            handlers = (
                [*publisher, *oa] if request.policy != "oa_first" else [*oa, *publisher]
            )
            if request.policy in (
                "grey_only",
                "scihub_only",
                "scihub_first",
                "fastest",
            ):
                from scansci_pdf.sources.libgen import try_libgen
                from scansci_pdf.sources.scihub import try_scihub

                grey = [("Sci-Hub", try_scihub), ("LibGen", try_libgen)]
                if request.policy == "scihub_only":
                    handlers = grey[:1]
                elif request.policy == "grey_only":
                    handlers = grey
                elif request.policy == "scihub_first":
                    handlers = [*grey, *handlers]
                else:
                    handlers.extend(grey)
            if request.policy not in ("grey_only", "scihub_only"):
                from scansci_pdf.sources.instsci import try_instsci

                if config.get("vpnsci_enabled"):
                    handlers.append(("WebVPN", try_instsci))
        seen: set[str] = set()
        for label, fn in handlers:
            if label in seen:
                continue
            seen.add(label)
            path = work_dir / f"source-{len(attempts)}.pdf"
            try:
                outcome = fn(identifier, path, dict(config))
            except Exception as exc:  # noqa: BLE001 - a failing legacy handler must not block the next source
                attempts.append(
                    {
                        "source": label,
                        "status": "failed",
                        "reason": "network_error",
                        "message": str(exc),
                    }
                )
                continue
            if isinstance(outcome, dict) and outcome.get("success") is not False:
                candidate = Path(outcome.get("file") or outcome.get("path") or path)
                if candidate.is_file():
                    with candidate.open("rb") as stream:
                        valid_pdf = stream.read(5) == b"%PDF-"
                else:
                    valid_pdf = False
                if valid_pdf:
                    attempts.append({"source": label, "status": "succeeded"})
                    return {
                        "path": candidate,
                        "source": outcome.get("source") or label,
                        "url": outcome.get("url"),
                        "metadata": outcome.get("metadata") or {},
                        "attempts": attempts,
                    }
            reason = (
                outcome.get("error_type", "not_found")
                if isinstance(outcome, dict)
                else "not_found"
            )
            message = (
                str(outcome.get("error") or outcome.get("reason") or "")
                if isinstance(outcome, dict)
                else ""
            )
            attempts.append(
                {
                    "source": label,
                    "status": "failed",
                    "reason": reason,
                    "message": message,
                }
            )
        return {
            "success": False,
            "attempts": attempts,
            "reason": next(
                (
                    item["reason"]
                    for item in reversed(attempts)
                    if item["reason"] not in ("not_found", "")
                ),
                "not_found",
            ),
        }
