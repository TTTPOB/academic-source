"""Shared acquisition use cases and a small, persistent single-process job runner."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from academic_source.domain import AcquisitionRequest, AcquisitionResult, Attempt, Job
from academic_source.infrastructure.documents import is_readable_pdf
from academic_source.infrastructure.storage import Store
from academic_source.settings import Settings
from academic_source.sources import LegacySources, Source
from academic_source.sources.lanes import ProviderLanes
from academic_source.sources.models import SourceFailure
from scansci_pdf.upstream_errors import RateLimited

from . import discovery, lists
from .downloads import DownloadCapabilities
from .exports import add_exports, add_markdown, add_supplementary
from .scheduler import PaperScheduler

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Application:
    def __init__(
        self,
        settings: Settings,
        store: Store | None = None,
        source: Source | None = None,
    ) -> None:
        self.settings = settings
        if settings.paper_workers > 1 and settings.source_config.get(
            "scihub_enabled", True
        ):
            raise ValueError(
                "paper_workers > 1 requires source_config.scihub_enabled=false; "
                "Sci-Hub runs its own browser threads"
            )
        self.store = store if store is not None else Store(settings)
        self._lanes: ProviderLanes | None = None
        self._scheduler: PaperScheduler | None = None
        self.downloads = DownloadCapabilities(self.store)
        self._lock = (self.store.root / ".application.lock").open("a+b")
        try:
            self._acquire_lock()
            (self.store.root / "sessions").mkdir(exist_ok=True)
            self.store.interrupt_jobs()
            config = self._config()
            if settings.paper_workers > 1:
                limits = config.get("host_concurrency")
                tabs = (
                    min(settings.browser_tabs, settings.paper_workers)
                    if str(config.get("browser_backend", "")).strip().lower() == "cdp"
                    else 1
                )
                self._lanes = ProviderLanes(
                    limits if isinstance(limits, dict) else {}, browser_workers=tabs
                )
            if source is not None:
                self.source = source
            elif self._lanes is not None:
                self.source = LegacySources(self._lanes)
            else:
                self.source = LegacySources()
            # Future.cancel() calls the cleanup callback on the cancelling thread.
            self._jobs_lock = RLock()
            self._futures: dict[str, Future[None]] = {}
            self._cancel_requested: set[str] = set()
            self._closed = False
            if self._lanes is None:
                self._executor = ThreadPoolExecutor(1, thread_name_prefix="acquisition")
                self._executor.submit(self.source.prepare, config)
            else:
                self._lanes.start(
                    partial(self.source.prepare, config), self.source.close
                )
                self._scheduler = PaperScheduler(
                    settings.paper_workers, self._run_paper, self._save
                )
        except BaseException:
            if self._lanes is not None:
                self._lanes.shutdown()
            if hasattr(self, "_executor"):
                self._executor.shutdown(wait=True, cancel_futures=True)
            self._lock.close()
            raise

    def _acquire_lock(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(
                f"Data directory {self.store.root} is already in use; "
                "use --server to connect to the running service"
            ) from exc

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return discovery.search(query, limit, config=self._config())

    def get_metadata(
        self,
        id_type: Literal["doi", "arxiv"],
        doi: str | None = None,
        arxiv_id: str | None = None,
    ) -> dict[str, Any]:
        return discovery.get_metadata(
            id_type, self._config(), doi=doi, arxiv_id=arxiv_id
        )

    def parse_list(
        self, *, upload_id: str | None = None, text: str | None = None
    ) -> list[dict[str, Any]]:
        return lists.parse_list(self.store, upload_id=upload_id, text=text)

    def submit(self, request: AcquisitionRequest) -> Job:
        if self._closed:
            raise RuntimeError("Application is closed")
        entries = (
            [
                {"identifier": discovery.normalize_identifier(value)}
                for value in request.identifiers
            ]
            if request.identifiers
            else self.parse_list(upload_id=request.upload_id, text=request.text)
        )
        if not entries:
            raise ValueError("The paper list is empty")
        now = _now()
        job = Job(
            id=uuid4().hex,
            request=request.model_copy(deep=True),
            total=len(entries),
            created_at=now,
            updated_at=now,
        )
        config = self._config()
        with self._jobs_lock:
            if self._closed:
                raise RuntimeError("Application is closed")
            if self._scheduler is not None:
                self._scheduler.submit(job, entries, config)
            else:
                self.store.save_job(job)
                future = self._executor.submit(self._run, job, entries, config)
                self._futures[job.id] = future
                future.add_done_callback(lambda _: self._forget_job(job.id))
        # Read a snapshot instead of exposing the mutable worker-owned model.
        return self.job(job.id)

    def _forget_job(self, job_id: str) -> None:
        with self._jobs_lock:
            self._futures.pop(job_id, None)
            self._cancel_requested.discard(job_id)

    def job(self, job_id: str) -> Job:
        if self._scheduler is not None:
            return self._scheduler.status(job_id) or self.store.get_job(job_id)
        with self._jobs_lock:
            job = self.store.get_job(job_id)
            if job.status == "queued":
                # Insertion order is submission order, including the running job.
                waiting = [
                    key for key, future in self._futures.items() if not future.done()
                ]
                if job_id in waiting:
                    job.queue_ahead = waiting.index(job_id)
            return job

    def cancel(self, job_id: str) -> Job:
        if self._scheduler is not None:
            return self._scheduler.cancel(job_id) or self.store.get_job(job_id)
        with self._jobs_lock:
            job = self.job(job_id)
            if job.status not in {"queued", "running"}:
                return job
            future = self._futures.get(job_id)
            if future is None:
                return job
            if future.cancel():
                job.status = "cancelled"
                job.queue_ahead = None
                self._save(job)
            else:
                self._cancel_requested.add(job_id)
            return job

    def wait(self, job_id: str, timeout: float = 0) -> Job:
        if self._scheduler is not None:
            future = self._scheduler.future(job_id)
        else:
            with self._jobs_lock:
                future = self._futures.get(job_id)
        if future:
            try:
                future.result(timeout=max(0, timeout))
            except (CancelledError, TimeoutError):
                pass
        return self.job(job_id)

    def close(self) -> None:
        with self._jobs_lock:
            if self._closed:
                return
            self._closed = True
        self.downloads.clear()
        try:
            if self._scheduler is not None:
                assert self._lanes is not None
                try:
                    self._scheduler.close()
                finally:
                    self._lanes.shutdown()
            else:
                try:
                    self._executor.submit(self.source.close).result()
                finally:
                    self._executor.shutdown(wait=True)
            self.store.interrupt_jobs()
        finally:
            self._lock.close()

    def export_artifacts(
        self, artifact_ids: list[str], output_dir: str
    ) -> list[dict[str, str | int]]:
        return self.store.export_artifacts(artifact_ids, output_dir)

    def _config(self) -> dict[str, Any]:
        from scansci_pdf.config import DEFAULT_CONFIG

        config = deepcopy({**DEFAULT_CONFIG, **self.settings.source_config})
        config.update(
            interactive=self.settings.interactive,
            auto_relogin=self.settings.interactive,
            cache_dir=str(self.store.root / "sessions"),
            output_dir=str(self.store.root / "work"),
        )
        if self.settings.paper_workers > 1:
            config["_parallel_http"] = True
        if not self.settings.interactive:
            config.update(
                browser_headless=True,
                scihub_browser_headless=True,
                scihub_turnstile_click=False,
            )
        return config

    def _run(
        self, job: Job, entries: list[dict[str, Any]], config: dict[str, Any]
    ) -> None:
        try:
            job.status = "running"
            job.started_at = _now()
            for entry in entries:
                # Publish the next paper's start atomically with the cancellation check.
                with self._jobs_lock:
                    if job.id in self._cancel_requested:
                        break
                    job.current_identifier = entry["identifier"]
                    self._save(job)
                result = self._run_paper(entry, job.request, job.id, config)
                job.results.append(result)
                job.completed += 1
                self._save(job)
            successes = sum(item.status == "succeeded" for item in job.results)
            job.status = (
                "succeeded"
                if successes == job.total
                else "partial"
                if successes
                else "failed"
            )
        except Exception:
            log.exception("Job %s failed", job.id)
            job.status = "failed"
            job.error = "Job execution failed; see server logs"
        finally:
            try:
                self.source.close()
            except Exception:
                log.exception("Source cleanup failed for job %s", job.id)
        with self._jobs_lock:
            if job.id in self._cancel_requested:
                job.status = "cancelled"
            job.current_identifier = ""
            self._save(job)

    def _run_paper(
        self,
        entry: dict[str, Any],
        request: AcquisitionRequest,
        job_id: str,
        config: dict[str, Any],
    ) -> AcquisitionResult:
        started = time.monotonic()
        try:
            if self._lanes is not None:
                self._lanes.wait_until_prepared()
            result = self._acquire(entry, request, job_id, config)
        except RateLimited as exc:
            result = AcquisitionResult(
                identifier=entry["identifier"],
                status="failed",
                reason="rate_limited",
                message=str(exc),
                action="retry_later",
                rate_limit=exc.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - a failed paper must not stop the job
            # Upstream exception text can include the full request URL.
            log.error(
                "Acquisition failed for %s (%s)",
                entry["identifier"],
                type(exc).__name__,
            )
            result = AcquisitionResult(
                identifier=entry["identifier"],
                status="failed",
                reason="internal_error",
                message="Acquisition failed; see server logs",
            )
        result.elapsed_seconds = round(time.monotonic() - started, 3)
        return result

    def _save(self, job: Job) -> None:
        job.updated_at = _now()
        self.store.save_job(job)

    def _acquire(
        self,
        entry: dict[str, Any],
        request: AcquisitionRequest,
        job_id: str,
        config: dict[str, Any],
    ) -> AcquisitionResult:
        identifier = entry["identifier"]
        if request.resolve_titles and not identifier.startswith(("10.", "arxiv:")):
            from scansci_pdf.resolver import resolve_title_to_doi

            doi = resolve_title_to_doi(identifier, config)
            if doi:
                identifier = discovery.normalize_identifier(doi)
        if not identifier.startswith(("10.", "arxiv:")):
            return AcquisitionResult(
                identifier=identifier,
                status="failed",
                reason="unsupported",
                message=(
                    "Not a DOI or arXiv ID, and no DOI was found for it as a "
                    "title. Find the DOI with search and submit that."
                ),
                action="provide_doi_or_arxiv_id",
            )
        if request.supplementary_only:
            result = AcquisitionResult(identifier=identifier, status="succeeded")
            with self._paper_workspace(job_id) as directory:
                result.cached = self._supplements(
                    result, request, Path(directory), config
                )
            return result

        key_data = {
            "identifier": identifier.lower(),
            "policy": request.policy,
            "config": self._cache_config(config),
            "source": f"{type(self.source).__module__}.{type(self.source).__qualname__}",
        }
        base_key = self._cache_key(key_data)
        requested = [kind for kind in ("markdown", "bibtex") if getattr(request, kind)]
        cached_pdf = self.store.get_cached(base_key)
        if not cached_pdf and not self.source.supports(identifier, request):
            return AcquisitionResult(
                identifier=identifier,
                status="failed",
                reason="unsupported",
                message="No source can fetch this identifier under the requested policy.",
            )
        with self._paper_workspace(job_id) as directory:
            work = Path(directory)
            if cached_pdf:
                result = cached_pdf.model_copy(deep=True, update={"cached": True})
                pdf = self.store.artifact_path(result.artifacts[0].id)
            else:
                outcome = self.source.acquire(
                    identifier, request, work, deepcopy(config)
                )
                if isinstance(outcome, SourceFailure):
                    return AcquisitionResult(
                        identifier=identifier,
                        status="failed",
                        reason=outcome.reason,
                        message=outcome.message,
                        action=outcome.action,
                        rate_limit=outcome.rate_limit,
                        attempts=outcome.attempts,
                    )
                pdf = outcome.path
                if not is_readable_pdf(pdf):
                    return AcquisitionResult(
                        identifier=identifier,
                        status="failed",
                        reason="invalid_document",
                        attempts=outcome.attempts,
                    )
                artifact = self.store.import_artifact(
                    pdf,
                    kind="pdf",
                    identifier=identifier,
                    source=outcome.source,
                    url=outcome.url,
                )
                result = AcquisitionResult(
                    identifier=identifier,
                    status="succeeded",
                    artifacts=[artifact],
                    attempts=outcome.attempts
                    or [Attempt(source=outcome.source, status="succeeded")],
                    metadata=self._public_metadata(outcome.metadata),
                )
                self.store.put_cached(base_key, result)
            cached_kinds = set()
            for kind in requested:
                key = self._export_key(result.artifacts[0].id, kind, config)
                cached = self.store.get_cached(key)
                if cached:
                    result.artifacts.extend(cached.artifacts)
                    result.warnings.extend(cached.warnings)
                    cached_kinds.add(kind)
            if requested:
                successful = add_exports(
                    result,
                    pdf,
                    request,
                    work,
                    config,
                    self.store,
                    cached_kinds,
                )
                for kind in successful:
                    artifacts = [item for item in result.artifacts if item.kind == kind]
                    self.store.put_cached(
                        self._export_key(result.artifacts[0].id, kind, config),
                        AcquisitionResult(
                            identifier=identifier,
                            status="succeeded",
                            artifacts=artifacts,
                            warnings=successful[kind],
                        ),
                    )
                result.cached = result.cached and set(requested) <= cached_kinds
            if request.supplementary:
                cached_supplements = self._supplements(result, request, work, config)
                result.cached = result.cached and cached_supplements
            return result

    def _paper_workspace(self, job_id: str) -> TemporaryDirectory:
        parent = self.store.root / "work" / job_id
        parent.mkdir(parents=True, exist_ok=True)
        return TemporaryDirectory(dir=parent, ignore_cleanup_errors=True)

    def _supplements(
        self,
        result: AcquisitionResult,
        request: AcquisitionRequest,
        work: Path,
        config: dict[str, Any],
    ) -> bool:
        # Supplements belong to the DOI, not to any particular main PDF.
        key = self._cache_key(
            {
                "supplementary_version": 1,
                "identifier": result.identifier.lower(),
                "config": self._cache_config(config),
            }
        )
        cached = self.store.get_cached(key)
        if cached is not None:
            result.artifacts.extend(cached.artifacts)
            result.warnings.extend(cached.warnings)
        else:
            warning_start = len(result.warnings)
            outcome = add_supplementary(
                result, work, config, self.store, lanes=self._lanes
            )
            if request.supplementary_only and outcome.failure:
                result.status = "failed"
                result.reason = outcome.failure
            if outcome.cacheable:
                self.store.put_cached(
                    key,
                    AcquisitionResult(
                        identifier=result.identifier,
                        status="succeeded",
                        artifacts=[
                            artifact
                            for artifact in result.artifacts
                            if artifact.kind == "supplementary"
                        ],
                        warnings=result.warnings[warning_start:],
                    ),
                )
        all_cached = cached is not None
        if request.markdown:
            for artifact in list(result.artifacts):
                if (
                    artifact.kind != "supplementary"
                    or artifact.media_type != "application/pdf"
                ):
                    continue
                markdown_key = self._export_key(artifact.id, "markdown", config)
                converted = self.store.get_cached(markdown_key)
                if converted is not None:
                    result.artifacts.extend(converted.artifacts)
                    result.warnings.extend(converted.warnings)
                    continue
                all_cached = False
                pdf = self.store.artifact_path(artifact.id)
                warnings = add_markdown(
                    result,
                    artifact,
                    pdf,
                    work / pdf.with_suffix(".md").name,
                    self.store,
                )
                if warnings is not None:
                    self.store.put_cached(
                        markdown_key,
                        AcquisitionResult(
                            identifier=result.identifier,
                            status="succeeded",
                            artifacts=[result.artifacts[-1]],
                            warnings=warnings,
                        ),
                    )
        return all_cached

    @staticmethod
    def _cache_config(config: dict[str, Any]) -> dict[str, Any]:
        # Exclude only values that cannot influence source selection or access.
        ignored = {
            "output_dir",
            "cache_dir",
            "parallel_sources",
            "oa_metadata_read_timeout",
            "science_reader_grace",
            "science_reader_timeout",
            "_parallel_http",
        }
        return {key: value for key, value in config.items() if key not in ignored}

    def _export_key(self, pdf_id: str, kind: str, config: dict[str, Any]) -> str:
        # BibTeX uses Crossref HTTP; Markdown conversion is local.
        options = {}
        if kind == "bibtex":
            options = {
                key: config.get(key)
                for key in (
                    "network_proxy",
                    "direct_domains",
                    "connect_timeout",
                    "read_timeout",
                )
            }
        return self._cache_key(
            {"export_version": 1, "pdf": pdf_id, "kind": kind, "config": options}
        )

    @staticmethod
    def _cache_key(data: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()

    @staticmethod
    def _public_metadata(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        allowed = ("doi", "title", "authors", "year", "journal", "abstract")
        return {
            key: value[key]
            for key in allowed
            if isinstance(value.get(key), (str, int, float, bool))
            or (
                key == "authors"
                and isinstance(value.get(key), list)
                and all(isinstance(author, str) for author in value[key])
            )
        }
