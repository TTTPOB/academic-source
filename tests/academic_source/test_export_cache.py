"""Independent export cache entries survive combinations and transient failures."""

import pytest

from academic_source.domain import AcquisitionRequest
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource


def test_successful_markdown_is_reused_when_bibtex_recovers(tmp_path, monkeypatch):
    from scansci_pdf import bibtex, md_export

    calls = {"markdown": 0, "bibtex": 0}

    def markdown(*args, **kwargs):
        calls["markdown"] += 1
        return "# Paper\n", ["Some sections could not be extracted"]

    def citation(*args):
        calls["bibtex"] += 1
        return None if calls["bibtex"] == 1 else "@article{paper}\n"

    monkeypatch.setattr(md_export, "pdf_to_markdown_detailed", markdown)
    monkeypatch.setattr(bibtex, "fetch_bibtex", citation)
    source = RecordingSource()
    app = Application(Settings(data_dir=tmp_path), source=source)

    def acquire(**options):
        request = AcquisitionRequest(identifiers=["10.1234/example"], **options)
        return app.wait(app.submit(request).id, 5).results[0]

    try:
        first = acquire(markdown=True, bibtex=True)
        second = acquire(markdown=True, bibtex=True)
        alone = acquire(markdown=True)
        assert [item.kind for item in first.artifacts] == ["pdf", "markdown"]
        assert [item.kind for item in second.artifacts] == ["pdf", "markdown", "bibtex"]
        assert alone.cached
        assert first.warnings == [
            "Some sections could not be extracted",
            "BibTeX unavailable",
        ]
        assert (
            second.warnings
            == alone.warnings
            == ["Some sections could not be extracted"]
        )
        assert first.artifacts[1].id == second.artifacts[1].id == alone.artifacts[1].id
        assert second.artifacts[2].id != second.artifacts[1].id
        assert calls == {"markdown": 1, "bibtex": 2}
        assert source.calls == ["10.1234/example"]
    finally:
        app.close()


def test_cache_config_keeps_access_settings_but_ignores_runtime_paths():
    config = {
        "output_dir": "/one",
        "cache_dir": "/sessions",
        "parallel_sources": 8,
        "science_reader_timeout": 60,
        "science_reader_grace": 5,
        "elsevier_api_key": "credential",
        "network_proxy": "proxy",
    }
    filtered = Application._cache_config(config)
    assert filtered == {"elsevier_api_key": "credential", "network_proxy": "proxy"}


def test_legacy_single_worker_setting_is_accepted_but_not_exposed():
    assert "job_workers" not in Settings.model_validate({"job_workers": 1}).model_dump()
    with pytest.raises(ValueError, match="job_workers"):
        Settings.model_validate({"job_workers": 2})


def test_partial_supplementary_import_does_not_cache_success(tmp_path, monkeypatch):
    from scansci_pdf import supplementary

    calls = 0

    def fetch(identifier, output_dir, config):
        nonlocal calls
        calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        first, missing = output_dir / "first.txt", output_dir / "missing.txt"
        first.write_text("present")
        return [str(first), str(missing)]

    monkeypatch.setattr(supplementary, "fetch_supplementary", fetch)
    app = Application(Settings(data_dir=tmp_path), source=RecordingSource())
    try:
        request = AcquisitionRequest(
            identifiers=["10.1234/example"], supplementary=True
        )
        for _ in range(2):
            result = app.wait(app.submit(request).id, 5).results[0]
            assert "Supplementary unavailable; see server logs" in result.warnings
        assert calls == 2
    finally:
        app.close()


def test_supplementary_empty_response_retries(tmp_path, monkeypatch):
    from scansci_pdf import supplementary

    calls = 0

    def fetch(*args):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(supplementary, "fetch_supplementary", fetch)
    app = Application(Settings(data_dir=tmp_path), source=RecordingSource())
    try:
        request = AcquisitionRequest(
            identifiers=["10.1234/example"], supplementary=True
        )
        for _ in range(2):
            result = app.wait(app.submit(request).id, 5).results[0]
            assert "No supplementary material retrieved" in result.warnings
        assert calls == 2
    finally:
        app.close()
