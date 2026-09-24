"""Existing publisher and access-channel knowledge behind one acquisition boundary."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Protocol

from ..domain import AcquisitionRequest
from ..infrastructure.documents import is_readable_pdf

Handler = tuple[str, Callable[[str, Path, dict[str, Any]], dict[str, Any] | None]]
_HTTP_RACE = frozenset(
    {
        "Unpaywall",
        "OpenAlexOA",
        "SemanticScholar",
        "OpenAIRE",
        "DOAJ",
        "EuropePMC",
        "CORE",
        "PMC",
        "OpenAlexContent",
    }
)


class Source(Protocol):
    def supports(self, identifier: str, request: AcquisitionRequest) -> bool: ...

    def acquire(
        self,
        identifier: str,
        request: AcquisitionRequest,
        work_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any] | None: ...


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


def _plan(
    doi: str, request: AcquisitionRequest, config: dict[str, Any]
) -> list[Handler]:
    """Enumerate original source families without invoking the old racing engine."""
    from scansci_pdf.sources.carsi_source import try_carsi
    from scansci_pdf.sources.core_api import try_core
    from scansci_pdf.sources.crossref import try_crossref_page_scrape
    from scansci_pdf.sources.europepmc import try_europepmc, try_pmc
    from scansci_pdf.sources.ezproxy import try_ezproxy
    from scansci_pdf.sources.oa_discovery import try_doaj
    from scansci_pdf.sources.openaire import try_openaire
    from scansci_pdf.sources.openalex import try_openalex_content_api, try_openalex_oa
    from scansci_pdf.sources.publishers import get_publisher_fast_sources
    from scansci_pdf.sources.semantic_scholar import try_semanticscholar
    from scansci_pdf.sources.unpaywall import try_unpaywall
    from scansci_pdf.sources.vpnsci import try_vpnsci

    publisher = [(label, fn) for fn, label in get_publisher_fast_sources(doi)]
    if (
        doi.startswith("10.1016/")
        and request.policy in ("fastest", "legal_only")
        and (config.get("elsevier_api_key") or os.environ.get("ELSEVIER_API_KEY"))
    ):
        publisher = [entry for entry in publisher if entry[0] == "ElsevierAPI"] + [
            entry for entry in publisher if entry[0] != "ElsevierAPI"
        ]
    oa: list[Handler] = [
        ("Unpaywall", try_unpaywall),
        ("OpenAlexOA", try_openalex_oa),
        ("SemanticScholar", try_semanticscholar),
        ("OpenAIRE", try_openaire),
        ("DOAJ", try_doaj),
        ("CrossrefPage", try_crossref_page_scrape),
        ("EuropePMC", try_europepmc),
        ("CORE", try_core),
        ("PMC", try_pmc),
    ]
    if config.get("openalex_api_key"):
        oa.append(("OpenAlexContent", try_openalex_content_api))
    legal = [*oa, *publisher] if request.policy == "oa_first" else [*publisher, *oa]

    grey: list[Handler] = []
    if config.get("scihub_enabled", True):
        from scansci_pdf.sources.libgen import try_libgen
        from scansci_pdf.sources.scibban import try_scibban
        from scansci_pdf.sources.scihub import try_scihub

        grey = [
            ("SciBban", try_scibban),
            ("LibGen", try_libgen),
            ("Sci-Hub", try_scihub),
        ]
    if request.policy == "scihub_only":
        return [entry for entry in grey if entry[0] == "Sci-Hub"]
    if request.policy == "grey_only":
        return grey

    institutional: list[Handler] = []
    if config.get("carsi_enabled") and config.get("carsi_idp_name"):
        institutional.append(("CARSI", try_carsi))
        if doi.startswith("10.1177/"):
            institutional.insert(0, ("SAGECN", _sage_cn_source))
    if config.get("vpnsci_enabled"):
        institutional.append(("WebVPN", try_vpnsci))
    if config.get("ezproxy_enabled"):
        institutional.append(("EZProxy", try_ezproxy))
    if institutional or config.get("elsevier_api_key"):
        institutional.extend(
            [
                ("SessionBroker", _broker_source),
                ("InstitutionalBrowser", _publisher_batch_source),
            ]
        )

    if request.policy == "scihub_first":
        ordered = [*grey, *legal, *institutional]
    elif request.policy == "legal_only":
        ordered = [*legal, *institutional]
    else:
        ordered = [*legal, *grey, *institutional]
    seen: set[str] = set()
    return [entry for entry in ordered if not (entry[0] in seen or seen.add(entry[0]))]


def _attempt(
    label: str,
    fn: Callable,
    doi: str,
    work_dir: Path,
    config: dict[str, Any],
    index: int,
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    path = work_dir / f"source-{index}.pdf"
    browser_diagnostics = None
    if label.endswith("Browser") and label != "InstitutionalBrowser":
        from scansci_pdf import _publisher_strategies_core

        browser_diagnostics = _publisher_strategies_core
        browser_diagnostics._clear_error()
    try:
        outcome = fn(doi, path, dict(config))
    except Exception as exc:  # noqa: BLE001 - site failures must not prevent the next source
        return None, {
            "source": label,
            "status": "failed",
            "reason": "network_error",
            "message": str(exc),
        }
    if isinstance(outcome, dict) and outcome.get("success") is not False:
        candidate = Path(outcome.get("file") or outcome.get("path") or path)
        if is_readable_pdf(candidate):
            return (
                {
                    "path": candidate,
                    "source": outcome.get("source") or label,
                    "url": outcome.get("url"),
                    "metadata": outcome.get("metadata") or {},
                },
                {"source": label, "status": "succeeded"},
            )
    if outcome is None and browser_diagnostics is not None:
        error_type, action = browser_diagnostics.get_last_error()
        if error_type:
            outcome = {"success": False, "error_type": error_type, "message": action}
    reason = (
        outcome.get("error_type") or outcome.get("reason") or "not_found"
        if isinstance(outcome, dict)
        else "not_found"
    )
    message = (
        str(outcome.get("error") or outcome.get("message") or "")
        if isinstance(outcome, dict)
        else ""
    )
    return None, {
        "source": label,
        "status": "failed",
        "reason": str(reason),
        "message": message,
    }


class LegacySources:
    """One scheduler for source handlers; browser operations remain on the job thread."""

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
        if self._arxiv(identifier):
            from scansci_pdf.sources.arxiv import download_arxiv_pdf

            def arxiv(reference: str, path: Path, source_config: dict[str, Any]):
                return download_arxiv_pdf(
                    f"https://arxiv.org/pdf/{reference.removeprefix('arxiv:')}.pdf",
                    path,
                    source_config,
                )

            handlers = [("arXiv", arxiv)]
        else:
            handlers = _plan(identifier, request, config)
        attempts: list[dict[str, str]] = []
        i = 0
        while i < len(handlers):
            label, fn = handlers[i]
            # Only HTTP-only OA handlers may race; all browser/session handlers
            # run on this owning worker thread, including publisher strategies.
            if (
                request.policy == "fastest"
                and config.get("parallel_sources", True)
                and label in _HTTP_RACE
            ):
                lane: list[tuple[int, Handler]] = []
                while i < len(handlers) and handlers[i][0] in _HTTP_RACE:
                    lane.append((i, handlers[i]))
                    i += 1
                if len(lane) > 1:
                    with ThreadPoolExecutor(max_workers=min(3, len(lane))) as pool:
                        remaining = iter(lane)

                        def start(entry):
                            pos, (name, handler) = entry
                            return pool.submit(
                                _attempt,
                                name,
                                handler,
                                identifier,
                                work_dir,
                                config,
                                pos,
                            )

                        tasks = {
                            start(next(remaining)) for _ in range(min(3, len(lane)))
                        }
                        found = None
                        while tasks:
                            done, tasks = wait(tasks, return_when=FIRST_COMPLETED)
                            for future in done:
                                result, attempt = future.result()
                                attempts.append(attempt)
                                if result and found is None:
                                    found = result
                            if found:
                                # Do not start more providers after success. In-flight HTTP
                                # calls finish before their temporary work directory is removed.
                                for future in tasks:
                                    future.cancel()
                                for future in tasks:
                                    if not future.cancelled():
                                        _, attempt = future.result()
                                        attempts.append(attempt)
                                return {**found, "attempts": attempts}
                            for _ in done:
                                entry = next(remaining, None)
                                if entry is not None:
                                    tasks.add(start(entry))
                    continue
                label, fn = lane[0][1]
                index = lane[0][0]
            else:
                index = i
                i += 1
            result, attempt = _attempt(label, fn, identifier, work_dir, config, index)
            attempts.append(attempt)
            if result:
                return {**result, "attempts": attempts}
        reason = (
            "cloudflare_blocked"
            if any(item["reason"] == "cloudflare_blocked" for item in attempts)
            else next(
                (
                    item["reason"]
                    for item in reversed(attempts)
                    if item["reason"] not in ("not_found", "")
                ),
                "not_found",
            )
        )
        reason = {
            "paywall": "auth_required",
            "login_required": "auth_required",
            "cloudflare_blocked": "network_error",
            "browser_unavailable": "unsupported",
            "config_needed": "unsupported",
        }.get(reason, reason)
        return {"success": False, "attempts": attempts, "reason": reason}
