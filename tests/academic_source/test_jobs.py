"""User-visible progress, bounded waiting, and optional-export retry semantics."""

from threading import Event, get_ident

from academic_source.domain import AcquisitionRequest
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource


def test_cdp_preflight_once_offline_does_not_block_http_job(
    tmp_path, monkeypatch, caplog
):
    from scansci_pdf import browser_backend

    calls = []
    main_thread = get_ident()

    def offline(config):
        calls.append(get_ident())
        raise ConnectionError("endpoint-token=secret")

    monkeypatch.setattr(browser_backend, "probe_cdp", offline)
    source = RecordingSource()
    application = Application(
        Settings(data_dir=tmp_path, source_config={"browser_backend": "cdp"}),
        source=source,
    )
    try:
        request = AcquisitionRequest(identifiers=["10.1234/first", "10.1234/second"])
        job = application.wait(application.submit(request).id, 5)
        assert job.status == "succeeded"
        assert source.calls == ["10.1234/first", "10.1234/second"]
        assert len(calls) == 1 and calls[0] != main_thread
        assert "CDP startup preflight failed" in caplog.text
        assert "endpoint-token" not in caplog.text
    finally:
        application.close()


def test_cdp_preflight_reports_actionable_configuration_error(monkeypatch, caplog):
    from scansci_pdf import browser_backend

    def invalid(config):
        raise RuntimeError("browser_backend=cdp requires source_config.browser_cdp_url")

    monkeypatch.setattr(browser_backend, "probe_cdp", invalid)
    Application._preflight_cdp({})
    assert "set source_config.browser_cdp_url" in caplog.text


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
