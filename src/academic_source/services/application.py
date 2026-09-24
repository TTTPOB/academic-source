"""Single-paper and batch acquisition through one persistent job loop."""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..domain import AcquisitionRequest, AcquisitionResult, Attempt, Job
from ..settings import Settings
from ..sources import LegacySources, Source


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: str) -> str:
    from scansci_pdf.identifiers import normalize_arxiv_id, normalize_doi

    value = value.strip()
    arxiv = normalize_arxiv_id(value)
    if arxiv:
        return f"arxiv:{arxiv}"
    return (
        normalize_doi(value)
        if value.lower().startswith(
            ("doi:", "http://doi.org/", "https://doi.org/", "https://dx.doi.org/")
        )
        else value
    )


class Application:
    def __init__(
        self, settings: Settings, store: Any = None, source: Source | None = None
    ) -> None:
        if store is None:
            from ..infrastructure.storage import Store

            store = Store(settings)
        self.settings = settings
        self.store = store
        self.source = source if source is not None else LegacySources()
        # One worker keeps legacy module-level diagnostics and browser page ownership together.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="academic-source"
        )
        self._futures: dict[str, Future[None]] = {}
        self._closed = False
        self.store.interrupt_jobs()

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        from scansci_pdf.search import search_papers

        fields = (
            "doi",
            "title",
            "authors",
            "year",
            "cited_by_count",
            "is_oa",
            "oa_url",
            "source",
        )
        return [
            {key: item[key] for key in fields if key in item}
            for item in search_papers(query, limit=limit)
        ]

    def resolve(self, identifier: str) -> dict[str, Any]:
        from scansci_pdf.identifiers import normalize_arxiv_id

        normalized = _identifier(identifier)
        if normalized.startswith("10.") or normalize_arxiv_id(normalized):
            return {
                "identifier": normalized,
                "doi": normalized if normalized.startswith("10.") else None,
            }
        from scansci_pdf.resolver import resolve_title_to_doi

        doi = resolve_title_to_doi(identifier, self._config())
        return {
            "identifier": _identifier(doi) if doi else identifier.strip(),
            "doi": doi,
            "title": identifier.strip(),
        }

    def parse_list(
        self, *, upload_id: str | None = None, text: str | None = None
    ) -> list[dict[str, Any]]:
        if bool(upload_id) == bool(text):
            raise ValueError("Provide exactly one of upload_id or text")
        if upload_id:
            path = self.store.upload_path(upload_id)
            from scansci_pdf.paperlist import parse_paper_list

            if path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, list):
                    raise ValueError("JSON paper list must be an array")
                return [self._entry(item) for item in payload]
            if path.suffix.lower() in (".txt", ".md"):
                return self.parse_list(text=path.read_text(encoding="utf-8-sig"))
            if path.suffix.lower() in (".csv", ".tsv", ".tab", ".xlsx"):
                from scansci_pdf.pipeline import entries_from_table, read_table

                rows = read_table(path)
                queue = entries_from_table(rows)
                titles = [
                    next(
                        (
                            str(value)
                            for key, value in row.items()
                            if "title" in key.lower() or "标题" in key or "题名" in key
                        ),
                        "",
                    )
                    for row in rows
                ]
                return [
                    {
                        "identifier": _identifier(
                            entry.identifier or title or entry.raw
                        ),
                        "title": title,
                        "raw": entry.raw,
                    }
                    for entry, title in zip(queue, titles)
                    if entry.identifier or title
                ]
            entries = parse_paper_list(path)
            return [
                {
                    "identifier": _identifier(entry.doi or entry.title or entry.raw),
                    "title": entry.title,
                    "doi": entry.doi,
                    "raw": entry.raw,
                }
                for entry in entries
                if entry.doi or entry.title or entry.raw
            ]
        from scansci_pdf.paperlist import parse_apa_references
        from scansci_pdf.pipeline import parse_queue

        assert text is not None
        if re.search(r"[A-Z][a-z\u00C0-\u024F]+,\s+[A-Z]\.", text):
            entries = parse_apa_references(text)
            return [
                {
                    "identifier": _identifier(item.doi or item.title or item.raw),
                    "title": item.title,
                    "doi": item.doi,
                    "raw": item.raw,
                }
                for item in entries
            ]
        return [
            {"identifier": _identifier(entry.identifier or entry.raw), "raw": entry.raw}
            for entry in parse_queue(text)
        ]

    @staticmethod
    def _entry(item: Any) -> dict[str, Any]:
        if isinstance(item, str):
            return {"identifier": _identifier(item)}
        if not isinstance(item, dict):
            raise TypeError("JSON entries must be strings or objects")
        value = item.get("identifier") or item.get("doi") or item.get("title")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("JSON entry requires identifier, doi or title")
        return {**item, "identifier": _identifier(value)}

    def submit(self, request: AcquisitionRequest) -> Job:
        if self._closed:
            raise RuntimeError("Application is closed")
        entries = self._entries(request)
        now = _now()
        job = Job(
            id=uuid4().hex,
            request=request,
            total=len(entries),
            created_at=now,
            updated_at=now,
        )
        self.store.save_job(job)
        self._futures[job.id] = self._executor.submit(
            self._run, job, entries, self._config()
        )
        return job

    def job(self, job_id: str) -> Job:
        return self.store.get_job(job_id)

    def wait(self, job_id: str, timeout: float = 0) -> Job:
        future = self._futures.get(job_id)
        if future:
            try:
                future.result(timeout=max(0, timeout))
            except TimeoutError:
                pass
        return self.job(job_id)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Queue cleanup after jobs so the browser closes on its owning worker thread.
        self._executor.submit(self._close_browser).result()
        self._executor.shutdown(wait=True)
        self.store.close()

    @staticmethod
    def _close_browser() -> None:
        from scansci_pdf.browser_engine import close_shared_browser

        close_shared_browser()

    def _entries(self, request: AcquisitionRequest) -> list[dict[str, Any]]:
        if request.identifiers:
            return [{"identifier": _identifier(value)} for value in request.identifiers]
        return self.parse_list(upload_id=request.upload_id, text=request.text)

    def _config(self) -> dict[str, Any]:
        from scansci_pdf.config import DEFAULT_CONFIG

        config = deepcopy({**DEFAULT_CONFIG, **self.settings.source_config})
        config.update(
            interactive=self.settings.interactive,
            auto_relogin=self.settings.interactive,
            cache_dir=str(self.settings.data_dir / "sessions"),
            output_dir=str(self.settings.data_dir / "work"),
        )
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
            self._save(job)
            for entry in entries:
                try:
                    result = self._acquire(entry, job.request, job.id, config)
                except Exception as exc:  # noqa: BLE001 - preserve a per-paper result on source or export failure
                    result = AcquisitionResult(
                        identifier=entry["identifier"],
                        status="failed",
                        reason="network_error",
                        message=str(exc),
                    )
                job.results.append(result)
                job.completed += 1
                job.artifacts.extend(result.artifacts)
                self._save(job)
            successes = sum(item.status == "succeeded" for item in job.results)
            job.status = (
                "succeeded"
                if successes == job.total
                else "partial"
                if successes
                else "failed"
            )
        except Exception as exc:  # noqa: BLE001 - preserve a per-paper result on source or export failure
            job.status = "failed"
            job.error = str(exc)
        self._save(job)

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
        if request.resolve_titles and not (identifier.startswith(("10.", "arxiv:"))):
            identifier = self.resolve(identifier)["identifier"]
        if not (identifier.startswith(("10.", "arxiv:"))):
            return AcquisitionResult(
                identifier=identifier,
                status="failed",
                reason="unsupported",
                message="Identifier could not be resolved",
            )
        key_data = {
            "identifier": identifier.lower(),
            "policy": request.policy,
            "markdown": request.markdown,
            "supplementary": request.supplementary,
            "bibtex": request.bibtex,
            "config": config,
            "source": f"{type(self.source).__module__}.{type(self.source).__qualname__}",
        }
        key = hashlib.sha256(
            json.dumps(key_data, sort_keys=True, default=str).encode()
        ).hexdigest()
        cached = self.store.get_cached(key)
        if cached:
            return cached.model_copy(update={"cached": True})
        if hasattr(self.source, "supports") and not self.source.supports(
            identifier, request
        ):
            return AcquisitionResult(
                identifier=identifier, status="failed", reason="unsupported"
            )
        work = self.store.root / "work" / job_id / uuid4().hex
        work.mkdir(parents=True, exist_ok=True)
        outcome = self.source.acquire(identifier, request, work, dict(config))
        attempts = [
            Attempt.model_validate(item) for item in (outcome or {}).get("attempts", [])
        ]
        if not outcome or outcome.get("success") is False:
            reason = (outcome or {}).get("reason", "not_found")
            return AcquisitionResult(
                identifier=identifier,
                status="failed",
                reason=reason,
                message=str((outcome or {}).get("message", "")),
                attempts=attempts,
            )
        path_value = outcome.get("path") or outcome.get("file")
        path = Path(path_value) if path_value else None
        if not path or not path.is_file() or not self._is_pdf(path):
            return AcquisitionResult(
                identifier=identifier,
                status="failed",
                reason="invalid_document",
                attempts=attempts,
            )
        source = str(outcome.get("source") or "unknown")
        artifact = self.store.import_artifact(
            path,
            kind="pdf",
            identifier=identifier,
            source=source,
            url=outcome.get("url"),
        )
        result = AcquisitionResult(
            identifier=identifier,
            status="succeeded",
            artifacts=[artifact],
            attempts=attempts or [Attempt(source=source, status="succeeded")],
            metadata=self._public_metadata(outcome.get("metadata")),
        )
        self._extras(result, path, request, work, config, source)
        self.store.put_cached(key, result)
        return result

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

    @staticmethod
    def _is_pdf(path: Path) -> bool:
        with path.open("rb") as stream:
            return stream.read(5) == b"%PDF-"

    def _extras(
        self,
        result: AcquisitionResult,
        path: Path,
        request: AcquisitionRequest,
        work: Path,
        config: dict[str, Any],
        source: str,
    ) -> None:
        identifier = result.identifier
        if request.markdown:
            try:
                from scansci_pdf.md_export import pdf_to_markdown_detailed

                md_path, warnings = pdf_to_markdown_detailed(path)
                result.warnings.extend(warnings)
                result.artifacts.append(
                    self.store.import_artifact(
                        Path(md_path),
                        kind="markdown",
                        identifier=identifier,
                        source=source,
                        derived_from=result.artifacts[0].id,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - preserve a per-paper result on source or export failure
                result.warnings.append(f"Markdown unavailable: {exc}")
        if request.bibtex and identifier.startswith("10."):
            try:
                from scansci_pdf.bibtex import fetch_bibtex

                content = fetch_bibtex(identifier, config)
                if content:
                    bib = work / "citation.bib"
                    bib.write_text(content, encoding="utf-8")
                    result.artifacts.append(
                        self.store.import_artifact(
                            bib, kind="bibtex", identifier=identifier, source="Crossref"
                        )
                    )
                else:
                    result.warnings.append("BibTeX unavailable")
            except Exception as exc:  # noqa: BLE001 - preserve a per-paper result on source or export failure
                result.warnings.append(f"BibTeX unavailable: {exc}")
        if request.supplementary and identifier.startswith("10."):
            try:
                from scansci_pdf.supplementary import fetch_supplementary

                for attachment in fetch_supplementary(
                    identifier, work / "supplementary", config
                ):
                    result.artifacts.append(
                        self.store.import_artifact(
                            Path(attachment),
                            kind="supplementary",
                            identifier=identifier,
                            source=source,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - preserve a per-paper result on source or export failure
                result.warnings.append(f"Supplementary unavailable: {exc}")
