"""SQLite catalog and local files for uploads, artifacts, jobs, and results."""

import mimetypes
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from academic_source.domain import AcquisitionResult, Artifact, Job, Provenance, Upload
from academic_source.settings import Settings

_UPLOAD_EXTENSIONS = {".txt", ".md", ".bib", ".csv", ".tsv", ".tab", ".xlsx", ".json"}
_CHUNK_SIZE = 1024 * 1024


class Store:
    def __init__(self, settings: Settings) -> None:
        self.root = settings.data_dir.expanduser()
        self.max_upload_bytes = settings.max_upload_bytes
        for folder in (
            self.root,
            self.root / "uploads",
            self.root / "artifacts",
            self.root / "work",
        ):
            folder.mkdir(parents=True, exist_ok=True)
        self._database = self.root / "catalog.sqlite"
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, record TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS result_cache (
                    key TEXT PRIMARY KEY, record TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Each operation owns its connection; worker threads never share one.
        with closing(sqlite3.connect(self._database, timeout=10)) as db, db:
            yield db

    def put_upload(self, filename: str, stream: BinaryIO) -> Upload:
        name = _safe_filename(filename)
        if Path(name).suffix.lower() not in _UPLOAD_EXTENSIONS:
            raise ValueError(f"Unsupported upload extension: {name}")
        upload_id = uuid4().hex
        destination = self.root / "uploads" / upload_id / name
        destination.parent.mkdir(parents=True)
        size = 0
        try:
            with destination.open("wb") as output:
                while chunk := stream.read(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise ValueError("Upload exceeds max_upload_bytes")
                    output.write(chunk)
            upload = Upload(id=upload_id, filename=name, size=size)
            with self._connect() as db:
                db.execute(
                    "INSERT INTO uploads (id, filename, size) VALUES (?, ?, ?)",
                    (upload.id, upload.filename, upload.size),
                )
        except Exception:
            destination.unlink(missing_ok=True)
            destination.parent.rmdir()
            raise
        return upload

    def upload_path(self, upload_id: str) -> Path:
        with self._connect() as db:
            row = db.execute(
                "SELECT filename FROM uploads WHERE id = ?", (upload_id,)
            ).fetchone()
        if row is None:
            raise KeyError(upload_id)
        return self.root / "uploads" / upload_id / row[0]

    def import_artifact(
        self,
        path: Path,
        *,
        kind: str,
        identifier: str | None,
        source: str,
        url: str | None = None,
        derived_from: str | None = None,
    ) -> Artifact:
        filename = _safe_filename(path.name)
        artifact_id = uuid4().hex
        destination = self.root / "artifacts" / artifact_id / filename
        destination.parent.mkdir(parents=True)
        try:
            shutil.copyfile(path, destination)
            artifact = Artifact(
                id=artifact_id,
                kind=kind,
                media_type=mimetypes.guess_type(filename)[0]
                or "application/octet-stream",
                filename=filename,
                size=destination.stat().st_size,
                identifier=identifier,
                provenance=Provenance(
                    source=source,
                    url=url,
                    acquired_at=datetime.now(UTC).isoformat(),
                    derived_from=derived_from,
                ),
            )
            with self._connect() as db:
                db.execute(
                    "INSERT INTO artifacts (id, record) VALUES (?, ?)",
                    (artifact.id, artifact.model_dump_json()),
                )
        except Exception:
            destination.unlink(missing_ok=True)
            destination.parent.rmdir()
            raise
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact:
        with self._connect() as db:
            row = db.execute(
                "SELECT record FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return Artifact.model_validate_json(row[0])

    def artifact_path(self, artifact_id: str) -> Path:
        artifact = self.get_artifact(artifact_id)
        return self.root / "artifacts" / artifact.id / artifact.filename

    def export_artifacts(
        self, artifact_ids: list[str], output_dir: str
    ) -> list[dict[str, str | int]]:
        output = Path(output_dir).expanduser().resolve()
        exported = []
        for artifact_id in artifact_ids:
            artifact = self.get_artifact(artifact_id)
            source = self.artifact_path(artifact_id)
            output.mkdir(parents=True, exist_ok=True)
            destination = output / f"{artifact.id}-{artifact.filename}"
            if source.resolve() != destination.resolve():
                shutil.copyfile(source, destination)
            exported.append(
                {
                    "id": artifact.id,
                    "path": str(destination),
                    "size": destination.stat().st_size,
                }
            )
        return exported

    def prune(self, *, apply: bool = False) -> dict[str, list[str]]:
        """Report or remove stale uploads, empty work dirs and unreferenced files."""
        cutoff = datetime.now(UTC) - timedelta(days=30)
        with self._connect() as db:
            uploads = db.execute("SELECT id, filename FROM uploads").fetchall()
            artifact_rows = db.execute("SELECT id, record FROM artifacts").fetchall()
            cache_rows = db.execute("SELECT key, record FROM result_cache").fetchall()
            jobs = [
                Job.model_validate_json(row[0])
                for row in db.execute("SELECT record FROM jobs")
            ]
            artifacts = {
                key: Artifact.model_validate_json(record)
                for key, record in artifact_rows
            }
            valid_cache = {}
            stale_cache = []
            for key, record in cache_rows:
                cached = AcquisitionResult.model_validate_json(record)
                if all(
                    item.id in artifacts
                    and (
                        self.root / "artifacts" / item.id / artifacts[item.id].filename
                    ).is_file()
                    for item in cached.artifacts
                ):
                    valid_cache[key] = cached
                else:
                    stale_cache.append(key)
            referenced = {item.id for job in jobs for item in job.artifacts} | {
                item.id for result in valid_cache.values() for item in result.artifacts
            }
            # Preserve the parent PDF of every referenced derived artifact.
            pending = list(referenced)
            while pending:
                artifact = artifacts.get(pending.pop())
                parent = artifact.provenance.derived_from if artifact else None
                if parent and parent not in referenced:
                    referenced.add(parent)
                    pending.append(parent)
            orphaned = sorted(artifacts.keys() - referenced)
            expired = [
                key
                for key, filename in uploads
                if (path := self.root / "uploads" / key / filename).is_file()
                and datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff
                and all(job.request.upload_id != key for job in jobs)
            ]
            empty_work = sorted(
                (
                    path
                    for path in (self.root / "work").rglob("*")
                    if path.is_dir() and not any(path.iterdir())
                ),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            report = {
                "expired_uploads": sorted(expired),
                "invalid_cache": sorted(stale_cache),
                "unreferenced_artifacts": orphaned,
                "empty_work_dirs": [str(path) for path in empty_work],
            }
            if apply:
                for key in stale_cache:
                    db.execute("DELETE FROM result_cache WHERE key = ?", (key,))
                for key in orphaned:
                    artifact = artifacts[key]
                    (self.root / "artifacts" / key / artifact.filename).unlink(
                        missing_ok=True
                    )
                    folder = self.root / "artifacts" / key
                    if folder.is_dir() and not any(folder.iterdir()):
                        folder.rmdir()
                    db.execute("DELETE FROM artifacts WHERE id = ?", (key,))
                for key in expired:
                    filename = next(
                        name for upload_id, name in uploads if upload_id == key
                    )
                    (self.root / "uploads" / key / filename).unlink(missing_ok=True)
                    (self.root / "uploads" / key).rmdir()
                    db.execute("DELETE FROM uploads WHERE id = ?", (key,))
                for path in empty_work:
                    if not any(path.iterdir()):
                        path.rmdir()
            return report

    def save_job(self, job: Job) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO jobs (id, record, status) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET record=excluded.record, status=excluded.status",
                (job.id, job.model_dump_json(exclude={"artifacts"}), job.status),
            )

    def get_job(self, job_id: str) -> Job:
        with self._connect() as db:
            row = db.execute(
                "SELECT record FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return Job.model_validate_json(row[0])

    def interrupt_jobs(self) -> None:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, record FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
            for job_id, record in rows:
                job = Job.model_validate_json(record)
                job.status = "interrupted"
                job.updated_at = datetime.now(UTC).isoformat()
                db.execute(
                    "UPDATE jobs SET record = ?, status = ? WHERE id = ?",
                    (job.model_dump_json(exclude={"artifacts"}), job.status, job_id),
                )

    def get_cached(self, key: str) -> AcquisitionResult | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT record FROM result_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            result = AcquisitionResult.model_validate_json(row[0])
            for artifact in result.artifacts:
                registered = db.execute(
                    "SELECT record FROM artifacts WHERE id = ?", (artifact.id,)
                ).fetchone()
                if registered is None:
                    return None
                saved = Artifact.model_validate_json(registered[0])
                if not (self.root / "artifacts" / saved.id / saved.filename).is_file():
                    return None
        return result

    def put_cached(self, key: str, result: AcquisitionResult) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO result_cache (key, record) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET record=excluded.record",
                (key, result.model_dump_json()),
            )


def _safe_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if name in {"", ".", ".."} or "\x00" in name:
        raise ValueError("Invalid filename")
    return name
