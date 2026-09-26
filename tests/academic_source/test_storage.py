"""Exercise persisted uploads, artifacts, jobs, and cache invalidation."""

from io import BytesIO
from pathlib import Path

import pytest

from academic_source.domain import AcquisitionRequest, AcquisitionResult, Job
from academic_source.infrastructure.storage import Store
from academic_source.settings import Settings


def test_upload_and_artifact_survive_reopen(tmp_path):
    settings = Settings(data_dir=tmp_path, max_upload_bytes=8)
    store = Store(settings)
    upload = store.put_upload(r"folder\refs.BIB", BytesIO(b"@book{}"))
    assert upload.filename == "refs.BIB"
    assert store.upload_path(upload.id).read_bytes() == b"@book{}"
    artifact = store.import_artifact(
        store.upload_path(upload.id),
        kind="bibtex",
        identifier="10.1/example",
        source="test",
    )

    reopened = Store(settings)
    assert reopened.upload_path(upload.id).read_bytes() == b"@book{}"
    assert reopened.get_artifact(artifact.id) == artifact
    assert reopened.artifact_path(artifact.id).read_bytes() == b"@book{}"
    with pytest.raises(KeyError):
        reopened.upload_path("missing")
    with pytest.raises(ValueError):
        reopened.put_upload("session.cookies", BytesIO(b"cookie"))
    with pytest.raises(ValueError, match="max_upload_bytes"):
        reopened.put_upload("large.csv", BytesIO(b"123456789"))
    assert not list((tmp_path / "uploads").glob("*/large.csv"))


def test_export_same_filename_keeps_both_artifacts(tmp_path):
    store = Store(Settings(data_dir=tmp_path / "data"))
    original = tmp_path / "paper.pdf"
    original.write_bytes(b"first")
    first = store.import_artifact(original, kind="pdf", identifier=None, source="test")
    original.write_bytes(b"second")
    second = store.import_artifact(original, kind="pdf", identifier=None, source="test")
    exported = store.export_artifacts([first.id, second.id], str(tmp_path / "output"))
    assert [Path(item["path"]).read_bytes() for item in exported] == [
        b"first",
        b"second",
    ]
    assert exported[0]["path"] != exported[1]["path"]


def test_restart_interrupts_only_unfinished_jobs(tmp_path):
    settings = Settings(data_dir=tmp_path)
    store = Store(settings)
    request = AcquisitionRequest(identifiers=["10.1/example"])
    for status in ("queued", "running", "succeeded"):
        store.save_job(
            Job(
                id=status,
                status=status,
                request=request,
                created_at="2026-01-01",
                updated_at="2026-01-01",
            )
        )

    restarted = Store(settings)
    assert restarted.get_job("queued").status == "queued"
    restarted.interrupt_jobs()
    assert restarted.get_job("queued").status == "interrupted"
    assert restarted.get_job("running").status == "interrupted"
    assert restarted.get_job("succeeded").status == "succeeded"
    assert restarted.get_job("running").updated_at != "2026-01-01"


def test_cache_miss_when_artifact_file_disappears(tmp_path):
    store = Store(Settings(data_dir=tmp_path))
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    artifact = store.import_artifact(
        source, kind="pdf", identifier="10.1/example", source="fixture"
    )
    result = AcquisitionResult(
        identifier="10.1/example", status="succeeded", artifacts=[artifact]
    )
    store.put_cached("paper:10.1/example", result)
    assert store.get_cached("paper:10.1/example") == result
    assert store.get_cached("unknown") is None

    store.artifact_path(artifact.id).unlink()
    assert store.get_cached("paper:10.1/example") is None
