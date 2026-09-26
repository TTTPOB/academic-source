"""User-visible progress, bounded waiting, and optional-export retry semantics."""

from threading import Event, Thread, get_ident

from academic_source.domain import AcquisitionRequest
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource


def test_source_lifecycle_stays_on_worker_thread(tmp_path):
    class TrackedSource(RecordingSource):
        def __init__(self):
            super().__init__()
            self.threads = []

        def prepare(self, config):
            self.threads.append(get_ident())

        def acquire(self, *args):
            self.threads.append(get_ident())
            return super().acquire(*args)

        def close(self):
            self.threads.append(get_ident())

    source = TrackedSource()
    application = Application(Settings(data_dir=tmp_path), source=source)
    job = application.wait(
        application.submit(AcquisitionRequest(identifiers=["10.1234/test"])).id, 5
    )
    application.close()
    assert job.status == "succeeded"
    assert len(source.threads) >= 3
    assert set(source.threads) == {source.threads[0]}
    assert source.threads[0] != get_ident()


def test_running_batch_exposes_completed_items_before_final_result(tmp_path):
    second_started, release = Event(), Event()

    class BlockingSource(RecordingSource):
        def acquire(self, identifier, request, work_dir, config):
            if identifier.endswith("second"):
                second_started.set()
                assert release.wait(5)
            return super().acquire(identifier, request, work_dir, config)

    application = Application(Settings(data_dir=tmp_path), source=BlockingSource())
    try:
        submitted = application.submit(
            AcquisitionRequest(identifiers=["10.1234/first", "10.1234/second"])
        )
        assert second_started.wait(5)
        current = application.wait(submitted.id, timeout=0)
        assert current.status == "running"
        assert current.completed == 1 and current.total == 2
        assert current.results[0].identifier == "10.1234/first"
        assert application.store.artifact_path(current.artifacts[0].id).is_file()
        release.set()
        assert application.wait(submitted.id, 5).status == "succeeded"
    finally:
        release.set()
        application.close()


def test_close_drains_accepted_jobs_before_worker_cleanup(tmp_path):
    started, release = Event(), Event()

    class BlockingSource(RecordingSource):
        def acquire(self, identifier, request, work_dir, config):
            if identifier.endswith("first"):
                started.set()
                assert release.wait(5)
            return super().acquire(identifier, request, work_dir, config)

    source = BlockingSource()
    application = Application(Settings(data_dir=tmp_path), source=source)
    first = application.submit(AcquisitionRequest(identifiers=["10.1234/first"]))
    assert started.wait(5)
    second = application.submit(AcquisitionRequest(identifiers=["10.1234/second"]))
    closed = Thread(target=application.close)
    closed.start()
    try:
        release.set()
        closed.join(timeout=5)
        assert not closed.is_alive()
        assert application.job(first.id).status == "succeeded"
        assert application.job(second.id).status == "succeeded"
        assert source.calls == ["10.1234/first", "10.1234/second"]
    finally:
        release.set()
        closed.join(timeout=5)


def test_failed_optional_export_retries_without_redownloading_pdf(
    tmp_path, monkeypatch
):
    from scansci_pdf import md_export

    calls = 0

    def export(pdf, write=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary converter failure")
        return "# Recovered export\n", []

    monkeypatch.setattr(md_export, "pdf_to_markdown_detailed", export)
    source = RecordingSource()
    application = Application(Settings(data_dir=tmp_path), source=source)
    request = AcquisitionRequest(identifiers=["10.1234/example"], markdown=True)
    try:
        first = application.wait(application.submit(request).id, 5).results[0]
        assert first.status == "succeeded" and first.warnings
        second = application.wait(application.submit(request).id, 5).results[0]
        assert [item.kind for item in second.artifacts] == ["pdf", "markdown"]
        assert (
            application.store.artifact_path(second.artifacts[1].id).read_text()
            == "# Recovered export\n"
        )
        assert second.artifacts[1].provenance.derived_from == first.artifacts[0].id
        third = application.wait(application.submit(request).id, 5).results[0]
        assert third.cached and third.artifacts == second.artifacts
        assert calls == 2 and source.calls == ["10.1234/example"]
        assert not list((tmp_path / "work").rglob("*.pdf"))
    finally:
        application.close()
