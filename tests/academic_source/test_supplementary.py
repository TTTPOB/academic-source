"""Supplementary acquisition results, provenance and cache behavior."""

import pytest

from academic_source.domain import AcquisitionRequest
from academic_source.infrastructure.storage import Store
from academic_source.interfaces.presentation import artifact_data
from academic_source.services.application import Application
from academic_source.settings import Settings
from scansci_pdf import supplementary as supp
from tests.academic_source.helpers import RecordingSource

DOI = "10.1234/missing"
PAGE = "https://publisher.example/article"
FILE = "https://publisher.example/supp_1.pdf"
CAPTION = "Exact caption text.\nSecond line."


def save_one(output_dir, path="paper_SI1.pdf"):
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / path
    output.write_bytes(b"fixture attachment")
    item = supp.SupplementaryItem(FILE, "Movie S1", CAPTION, "Original name.pdf")
    return supp.SavedSupplementary(output, item, PAGE)


@pytest.fixture
def acquire(tmp_path):
    source = RecordingSource()
    app = Application(Settings(data_dir=tmp_path), source=source)

    def run(identifier=DOI, **flags):
        request = AcquisitionRequest(identifiers=[identifier], **flags)
        job = app.wait(app.submit(request).id, 5)
        return job.results[0]

    try:
        yield run, source
    finally:
        app.close()


def test_supplementary_only_runs_without_main_pdf_and_preserves_note(
    monkeypatch, acquire
):
    run, source = acquire

    def fetch(doi, output_dir, config, **options):
        return supp.SupplementaryResult(pages_read=1, saved=[save_one(output_dir)])

    monkeypatch.setattr(supp, "fetch_supplementary", fetch)
    result = run(supplementary_only=True)
    assert source.calls == []
    assert result.status == "succeeded" and not result.cached
    (item,) = result.artifacts
    assert item.kind == "supplementary"
    assert (
        item.note
        == f"Movie S1\n{CAPTION}\nOriginal file: Original name.pdf\nSource page: {PAGE}"
    )
    assert artifact_data(item)["note"] == item.note
    assert item.provenance.url == FILE
    assert run().reason == "not_found"


@pytest.mark.parametrize(
    ("pages_read", "items", "status", "reason", "leading"),
    [
        (
            0,
            [],
            "failed",
            "supplementary_page_unavailable",
            1,
        ),
        (1, [], "succeeded", "", 1),
        (
            1,
            ["too large (200,000,001 bytes)", "HTTP 404", "not a file"],
            "failed",
            "supplementary_download_failed",
            0,
        ),
    ],
)
def test_supplementary_only_status_and_warnings(
    monkeypatch, acquire, pages_read, items, status, reason, leading
):
    run, source = acquire
    unsaved = [
        supp.UnsavedSupplementary(
            supp.SupplementaryItem(f"{FILE}?signature=fixture", "Data S1"), problem
        )
        for problem in items
    ]
    monkeypatch.setattr(
        supp,
        "fetch_supplementary",
        lambda *_args, **_kwargs: supp.SupplementaryResult(
            pages_read=pages_read,
            unsaved=unsaved,
        ),
    )
    result = run(supplementary_only=True)
    assert source.calls == []
    assert (result.status, result.reason) == (status, reason)
    # One summary line when nothing was listed, then one line per missing file
    # naming the file, its public URL, and the reason.
    assert len(result.warnings) == leading + len(items)
    for warning, problem in zip(result.warnings[leading:], items, strict=True):
        assert "Data S1" in warning and FILE in warning and problem in warning
        assert "signature" not in warning


def test_cached_supplementary_markdown_is_independent_of_main_pdf(monkeypatch, acquire):
    run, source = acquire
    calls = []

    def fetch(doi, output_dir, config, **options):
        calls.append("fetch")
        return supp.SupplementaryResult(
            pages_read=1,
            source="PMC",
            route="PMC",
            pmcid="PMC123",
            saved=[save_one(output_dir)],
        )

    def convert(pdf, *, write):
        calls.append(pdf.name)
        if pdf.name == "paper_SI1.pdf" and calls.count(pdf.name) == 1:
            raise ValueError("conversion failed")
        return "# Offline Markdown\n", []

    monkeypatch.setattr(supp, "fetch_supplementary", fetch)
    monkeypatch.setattr("scansci_pdf.md_export.pdf_to_markdown_detailed", convert)
    doi = "10.1234/example"
    assert run(doi).status == "succeeded"  # Cache the main PDF alone.
    only = run(doi, supplementary_only=True)
    failed_conversion = run(doi, supplementary_only=True, markdown=True)
    assert failed_conversion.status == "succeeded"
    (warning,) = failed_conversion.warnings  # Only the failed conversion is reported.
    assert "paper_SI1.pdf" in warning
    combined = run(doi, supplementary=True, markdown=True)
    assert [item.kind for item in combined.artifacts] == [
        "pdf",
        "markdown",
        "supplementary",
        "markdown",
    ]
    main, main_md, supplementary, supp_md = combined.artifacts
    assert main_md.provenance.derived_from == main.id
    assert supplementary.id == only.artifacts[0].id
    assert supplementary.note.endswith("Obtained from PMC (PMC123)")
    assert supp_md.provenance.derived_from == supplementary.id
    assert supp_md.provenance.source == "PMC"
    assert supp_md.note == "Converted from supplementary file: paper_SI1.pdf"
    again = run(doi, supplementary_only=True, markdown=True)
    assert again.cached and again.artifacts == [supplementary, supp_md]
    main_only = run(doi, markdown=True)
    assert main_only.cached and main_only.artifacts == [main, main_md]
    assert calls == ["fetch", "paper_SI1.pdf", "paper.pdf", "paper_SI1.pdf"]
    assert source.calls == [doi]


def test_artifact_without_note_loads_and_omits_empty_note(tmp_path):
    store = Store(Settings(data_dir=tmp_path))
    path = tmp_path / "old.pdf"
    path.write_bytes(b"old fixture")
    created = store.import_artifact(path, kind="pdf", identifier=DOI, source="offline")
    with store._connect() as db:
        db.execute(
            "UPDATE artifacts SET record = ? WHERE id = ?",
            (
                created.model_dump_json(exclude={"note"}),
                created.id,
            ),
        )
    old = store.get_artifact(created.id)
    assert old.note == ""
    assert "note" not in artifact_data(old)
