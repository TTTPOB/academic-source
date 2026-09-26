"""Pruning preserves existing jobs and valid cache references."""

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from os import utime

from academic_source.domain import AcquisitionRequest, AcquisitionResult, Job
from academic_source.interfaces import cli
from academic_source.services.application import Application
from academic_source.settings import Settings


def test_prune_dry_run_and_apply_preserve_referenced_files(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ACADEMIC_SOURCE_DATA_DIR", str(tmp_path))
    app = Application(Settings(data_dir=tmp_path))
    store = app.store
    source = tmp_path / "document.pdf"
    source.write_bytes(b"pdf")
    referenced = store.import_artifact(
        source, kind="pdf", identifier=None, source="test"
    )
    derived = store.import_artifact(
        source,
        kind="markdown",
        identifier=None,
        source="test",
        derived_from=referenced.id,
    )
    orphan = store.import_artifact(source, kind="pdf", identifier=None, source="test")
    store.put_cached(
        "valid",
        AcquisitionResult(
            identifier="10.1/test", status="succeeded", artifacts=[derived]
        ),
    )
    store.put_cached(
        "invalid",
        AcquisitionResult(
            identifier="10.1/test", status="succeeded", artifacts=[orphan]
        ),
    )
    store.artifact_path(orphan.id).unlink()
    (tmp_path / "artifacts" / orphan.id).rmdir()
    old = store.put_upload("old.txt", BytesIO(b"old"))
    referenced_upload = store.put_upload("listed.txt", BytesIO(b"listed"))
    age = (datetime.now(UTC) - timedelta(days=31)).timestamp()
    for upload in (old, referenced_upload):
        utime(store.upload_path(upload.id), (age, age))
    job = Job(
        id="existing",
        request=AcquisitionRequest(upload_id=referenced_upload.id),
        created_at="2026-01-01",
        updated_at="2026-01-01",
    )
    store.save_job(job)
    app.close()

    assert cli.main(["prune"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["applied"] is False
    assert old.id in preview["expired_uploads"]
    assert referenced_upload.id not in preview["expired_uploads"]
    assert "invalid" in preview["invalid_cache"]
    assert orphan.id in preview["unreferenced_artifacts"]
    assert store.upload_path(old.id).is_file()
    assert cli.main(["prune", "--apply"]) == 0
    capsys.readouterr()
    assert not (tmp_path / "uploads" / old.id).exists()
    assert store.upload_path(referenced_upload.id).is_file()
    assert store.artifact_path(referenced.id).is_file()
    assert store.artifact_path(derived.id).is_file()
    assert store.get_job(job.id).id == job.id


def test_prune_refuses_data_dir_held_by_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACADEMIC_SOURCE_DATA_DIR", str(tmp_path))
    app = Application(Settings(data_dir=tmp_path))
    try:
        assert cli.main(["prune", "--apply"]) == 1
        assert "--server" in capsys.readouterr().err
    finally:
        app.close()
