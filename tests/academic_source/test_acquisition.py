"""Exercise persisted job outcomes, source normalization, caching, and login policy."""

from __future__ import annotations

from academic_source.domain import AcquisitionRequest
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import paper_bytes


class FixtureSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def acquire(self, identifier, request, work_dir, config):
        self.calls.append((identifier, request.policy))
        if identifier.endswith("missing"):
            return None
        if identifier.endswith("paywall"):
            return {
                "success": False,
                "reason": "auth_required",
                "attempts": [
                    {
                        "source": "publisher",
                        "status": "failed",
                        "reason": "auth_required",
                    }
                ],
            }
        paper = work_dir / "paper.pdf"
        paper.write_bytes(
            b"<html>not a paper</html>"
            if identifier.endswith("html")
            else b"%PDF-1.7\ntruncated document"
            if identifier.endswith("truncated")
            else paper_bytes()
        )
        return {
            "path": paper,
            "source": "fixture",
            "url": "https://example.org/article",
            "metadata": {
                "title": "A paper",
                "file": str(paper),
                "output_dir": str(work_dir),
            },
        }


def test_batch_persists_each_result_and_rejects_failure_dict_and_html(tmp_path):
    source = FixtureSource()
    app = Application(Settings(data_dir=tmp_path), source=source)
    try:
        request = AcquisitionRequest(
            identifiers=[
                "10.1234/good",
                "10.1234/missing",
                "10.1234/paywall",
                "10.1234/html",
                "10.1234/truncated",
            ]
        )
        job = app.wait(app.submit(request).id, timeout=5)
        assert job.status == "partial" and job.completed == job.total == 5
        assert [item.reason for item in job.results] == [
            "",
            "not_found",
            "auth_required",
            "invalid_document",
            "invalid_document",
        ]
        assert len(job.artifacts) == 1
        assert job.results[0].metadata == {"title": "A paper"}
        assert (
            app.store.artifact_path(job.artifacts[0].id)
            .read_bytes()
            .startswith(b"%PDF-")
        )
        assert job.results[2].attempts[0].reason == "auth_required"
        assert app.job(job.id).results == job.results
    finally:
        app.close()


def test_cache_respects_policy_and_missing_file(tmp_path):
    source = FixtureSource()
    app = Application(Settings(data_dir=tmp_path), source=source)
    try:

        def get(policy):
            return app.wait(
                app.submit(
                    AcquisitionRequest(identifiers=["10.1234/good"], policy=policy)
                ).id,
                5,
            ).results[0]

        first = get("legal_only")
        assert first.status == "succeeded" and not first.cached
        second = get("legal_only")
        assert second.cached and second.artifacts[0].id == first.artifacts[0].id
        other = get("oa_first")
        assert not other.cached and len(source.calls) == 2
        app.store.artifact_path(first.artifacts[0].id).unlink()
        replaced = get("legal_only")
        assert not replaced.cached and replaced.artifacts[0].id != first.artifacts[0].id
    finally:
        app.close()


def test_uploaded_list_and_inline_text_share_identifier_resolution(tmp_path):
    from io import BytesIO

    app = Application(Settings(data_dir=tmp_path), source=FixtureSource())
    try:
        upload = app.store.put_upload(
            "list.txt", BytesIO(b"https://doi.org/10.1234/good\n10.1234/missing\n")
        )
        parsed = app.parse_list(upload_id=upload.id)
        assert [entry["identifier"] for entry in parsed] == [
            "10.1234/good",
            "10.1234/missing",
        ]
        inline = app.parse_list(text="https://doi.org/10.1234/good\n10.1234/missing\n")
        assert [entry["identifier"] for entry in inline] == [
            "10.1234/good",
            "10.1234/missing",
        ]
        job = app.wait(app.submit(AcquisitionRequest(upload_id=upload.id)).id, 5)
        assert [item.status for item in job.results] == ["succeeded", "failed"]
        table = app.store.put_upload(
            "papers.csv", BytesIO(b"doi,title\n10.1234/good,Known\n,Title Only\n")
        )
        assert [item["identifier"] for item in app.parse_list(upload_id=table.id)] == [
            "10.1234/good",
            "Title Only",
        ]
    finally:
        app.close()


def test_optional_exports_fail_without_downgrading_pdf(monkeypatch, tmp_path):
    from scansci_pdf import bibtex, md_export, supplementary

    def conversion_failed(*args, **kwargs):
        raise RuntimeError("converter unavailable")

    monkeypatch.setattr(md_export, "pdf_to_markdown_detailed", conversion_failed)
    monkeypatch.setattr(bibtex, "fetch_bibtex", lambda *args: None)
    monkeypatch.setattr(supplementary, "fetch_supplementary", lambda *args: [])
    app = Application(Settings(data_dir=tmp_path), source=FixtureSource())
    try:
        request = AcquisitionRequest(
            identifiers=["10.1234/good"], markdown=True, bibtex=True, supplementary=True
        )
        result = app.wait(app.submit(request).id, 5).results[0]
        assert result.status == "succeeded"
        assert [artifact.kind for artifact in result.artifacts] == ["pdf"]
        assert any("Markdown unavailable" in warning for warning in result.warnings)
        assert "BibTeX unavailable" in result.warnings
    finally:
        app.close()


def test_source_adapter_preserves_failed_attempts_and_publisher_order(
    monkeypatch, tmp_path
):
    from academic_source.sources import LegacySources
    from scansci_pdf.sources import publishers

    def rejected(identifier, path, config):
        return {"success": False, "error_type": "auth_required", "error": "paywall"}

    def available(identifier, path, config):
        path.write_bytes(paper_bytes())
        return {"success": True, "file": str(path), "source": "publisher"}

    monkeypatch.setattr(
        publishers,
        "get_publisher_fast_sources",
        lambda doi: [(rejected, "Direct"), (available, "Publisher")],
    )
    outcome = LegacySources().acquire(
        "10.1234/test",
        AcquisitionRequest(identifiers=["10.1234/test"], policy="legal_only"),
        tmp_path,
        {"vpnsci_enabled": False},
    )
    assert outcome["source"] == "publisher"
    assert [item["reason"] for item in outcome["attempts"][:1]] == ["auth_required"]
    assert [item["source"] for item in outcome["attempts"]] == ["Direct", "Publisher"]


def test_noninteractive_legacy_login_fallback_does_not_open_windows(
    monkeypatch, tmp_path
):
    from scansci_pdf import _publisher_strategies_core as publisher

    def failed_headless(*args):
        publisher._set_error("browser_unavailable", "try_other_source")
        return False

    def no_window(*args, **kwargs):
        raise AssertionError("visible browser launched without user opt-in")

    monkeypatch.setattr(publisher, "_HAS_CLOAKBROWSER", True)
    monkeypatch.setattr(publisher, "_browser_download", failed_headless)
    monkeypatch.setattr(publisher, "_browser_download_visible", no_window)
    assert (
        publisher._browser_download_with_fallback(
            "10.1234/test",
            "url",
            tmp_path / "paper.pdf",
            {"interactive": False},
            "Wiley",
        )
        is False
    )
