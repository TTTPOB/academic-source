"""Existing publisher and access-channel knowledge behind one acquisition boundary."""

from __future__ import annotations

import logging
import time
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from ..domain import AcquisitionRequest, Attempt
from ..infrastructure.documents import looks_like_preprint
from .errors import SourceError
from .lanes import ProviderLanes
from .legacy import LegacyHandler, call_legacy
from .models import (
    ProviderOutcome,
    SourceFailure,
    SourceSuccess,
    failed_attempt,
    observe,
)
from .oa import PREPRINT_WARNING, acquire_oa, acquire_preprint
from .oa_candidates import is_preprint_doi
from .registry import FAST_POLICIES, PROVIDERS
from .registry import plan as _plan


class Source(Protocol):
    def prepare(self, config: dict[str, Any]) -> None: ...

    def close(self) -> None: ...

    def supports(self, identifier: str, request: AcquisitionRequest) -> bool: ...

    def acquire(
        self,
        identifier: str,
        request: AcquisitionRequest,
        work_dir: Path,
        config: dict[str, Any],
    ) -> SourceSuccess | SourceFailure: ...


def _attempt(
    label: str,
    fn: LegacyHandler,
    doi: str,
    work_dir: Path,
    config: dict[str, Any],
    index: int,
    hold_preprints: bool = False,
) -> tuple[ProviderOutcome, Attempt]:
    started = time.monotonic()
    result = call_legacy(label, fn, doi, work_dir / f"source-{index}.pdf", config)
    if not isinstance(result, SourceSuccess):
        attempt = failed_attempt(label, result)
    elif hold_preprints and looks_like_preprint(result.path):
        attempt = Attempt(source=label, status="preprint")
    else:
        attempt = Attempt(source=label, status="succeeded")
    return result, observe(doi, attempt, started)


class LegacySources:
    """Try source handlers in policy order until a readable PDF is found."""

    def __init__(self, lanes: ProviderLanes | None = None) -> None:
        self._lanes = lanes

    def prepare(self, config: dict[str, Any]) -> None:
        if str(config.get("browser_backend", "")).strip().lower() != "cdp":
            return
        from scansci_pdf.browser_backend import CDPSetupError, probe_cdp

        try:
            probe_cdp(config)
        except Exception as exc:  # noqa: BLE001 - optional preflight must not block jobs
            if isinstance(exc, CDPSetupError):
                reason = str(exc)
            elif isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError":
                reason = "connection timeout"
            else:
                reason = "connection unavailable"
            logging.getLogger(__name__).warning(
                "CDP startup preflight failed (%s); browser requests will retry on demand",
                reason,
            )

    def close(self) -> None:
        from scansci_pdf.browser_engine import close_shared_browser

        close_shared_browser()

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
    ) -> SourceSuccess | SourceFailure:
        config = dict(config)
        attempts: list[Attempt] = []
        errors: list[SourceError] = []
        closed_access = False
        preprints = []
        held: SourceSuccess | None = None
        # Asking for a preprint means the preprint is the wanted version.
        hold_preprints = not self._arxiv(identifier) and not is_preprint_doi(identifier)
        if not self._arxiv(identifier) and request.policy in FAST_POLICIES:
            config["_acquisition_attempted_urls"] = set()
            oa_result = (
                acquire_oa(
                    identifier, work_dir, config, run_browser=self._lanes.browser
                )
                if self._lanes is not None
                else acquire_oa(identifier, work_dir, config)
            )
            if isinstance(oa_result, SourceSuccess):
                return oa_result
            attempts.extend(oa_result.attempts)
            errors.extend(oa_result.errors)
            closed_access = oa_result.closed_access
            preprints = oa_result.preprints
            held = oa_result.held_preprint
        if self._arxiv(identifier):
            handlers = [("arXiv", PROVIDERS["arXiv"].resolve())]
        else:
            handlers = _plan(identifier, request, config, closed_access)
        for index, (label, fn) in enumerate(handlers):
            call = partial(
                _attempt, label, fn, identifier, work_dir, config, index, hold_preprints
            )
            result, attempt = (
                self._lanes.attempt(label, PROVIDERS[label], call)
                if self._lanes is not None
                else call()
            )
            attempts.append(attempt)
            if attempt.status == "preprint" and isinstance(result, SourceSuccess):
                held = held or result
                continue
            if isinstance(result, SourceSuccess):
                result.attempts = attempts
                return result
            errors.append(result)
        if held is not None:
            held.attempts = attempts
            held.warnings.append(PREPRINT_WARNING)
            return held
        if preprints:
            late = acquire_preprint(identifier, preprints, work_dir, config)
            attempts.extend(late.attempts)
            if isinstance(late, SourceSuccess):
                late.attempts = attempts
                return late
            errors.extend(late.errors)
        return SourceFailure.summarize(errors, attempts)
