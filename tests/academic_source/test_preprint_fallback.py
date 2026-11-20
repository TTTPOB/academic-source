"""Preprints are fetched only when no other version of a paper can be."""

from __future__ import annotations

import pytest

from academic_source import sources
from academic_source.domain import AcquisitionRequest
from academic_source.sources import oa
from academic_source.sources.models import SourceFailure, SourceSuccess
from academic_source.sources.oa_candidates import (
    Candidate,
    is_preprint_doi,
    openalex_candidates,
    unpaywall_candidates,
)
from tests.academic_source.helpers import isolate_upstreams
from tests.academic_source.test_oa_fast_path import Response, pdf_bytes

DOI = "10.1371/journal.pbio.3002000"
PREPRINT = "https://www.biorxiv.org/content/biorxiv/early/2022/04/19/x.full.pdf"
PUBLISHED = "https://journals.plos.org/plosbiology/article/file?id=x&type=printable"


@pytest.fixture(autouse=True)
def offline_upstreams(monkeypatch):
    isolate_upstreams(monkeypatch)


@pytest.mark.parametrize(
    ("doi", "expected"),
    [
        ("10.1101/2022.04.19.488821", True),
        ("10.1101/199380", True),
        ("10.64898/2026.01.02.123456", True),
        ("10.21203/rs.3.rs-3836994/v1", True),
        ("10.48550/arXiv.2401.01234", True),
        ("10.1101/gr.243386.118", False),
        ("10.1101/gad.1234.5", False),
        (DOI, False),
    ],
)
def test_preprint_dois(doi, expected):
    assert is_preprint_doi(doi) is expected


def test_unpaywall_puts_preprints_after_every_other_version():
    payload = {
        "best_oa_location": {
            "url_for_pdf": PREPRINT,
            "version": "submittedVersion",
            "host_type": "repository",
        },
        "oa_locations": [
            {
                "url_for_pdf": PUBLISHED,
                "version": "publishedVersion",
                "host_type": "publisher",
            },
            {
                "url_for_pdf": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/pdf/a.pdf",
                "version": "acceptedVersion",
                "host_type": "repository",
            },
        ],
    }
    candidates = unpaywall_candidates(payload)
    assert [c.url for c in candidates if not c.preprint] == [
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/pdf/a.pdf",
        PUBLISHED,
    ]
    assert [c.url for c in candidates if c.preprint] == [PREPRINT]
    assert candidates[-1].preprint


def test_openalex_marks_preprints_by_version_or_host():
    payload = {
        "locations": [
            {"is_oa": True, "pdf_url": PREPRINT, "version": None},
            {"is_oa": True, "pdf_url": PUBLISHED, "version": "publishedVersion"},
        ],
        "open_access": {"oa_url": "https://arxiv.org/pdf/1710.02563"},
    }
    flags = {c.url: c.preprint for c in openalex_candidates(payload)}
    assert flags == {
        PREPRINT: True,
        PUBLISHED: False,
        "https://arxiv.org/pdf/1710.02563": True,
    }


def _discover(*candidates):
    return lambda *args: iter([oa.Discovery("Unpaywall", list(candidates))])


def test_published_copy_wins_over_an_earlier_preprint(monkeypatch, tmp_path):
    visited = []
    monkeypatch.setattr(
        oa,
        "discover",
        _discover(
            Candidate(PREPRINT, "Unpaywall", preprint=True),
            Candidate(PUBLISHED, "Unpaywall"),
        ),
    )

    def fetch(url, *args, **kwargs):
        visited.append(url)
        return Response(url, body=pdf_bytes())

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa(DOI, tmp_path, {})
    assert isinstance(result, SourceSuccess)
    assert result.url == PUBLISHED and not result.warnings
    assert visited == [PUBLISHED]


def test_preprint_doi_takes_the_preprint_directly(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oa, "discover", _discover(Candidate(PREPRINT, "Unpaywall", preprint=True))
    )
    monkeypatch.setattr(
        oa, "fetch", lambda url, *args, **kwargs: Response(url, body=pdf_bytes())
    )
    result = oa.acquire_oa("10.1101/2022.04.19.488821", tmp_path, {})
    assert isinstance(result, SourceSuccess) and not result.warnings


def test_preprint_is_the_last_resort_after_every_source(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(
        oa,
        "discover",
        _discover(
            Candidate(PREPRINT, "Unpaywall", preprint=True),
            Candidate(PUBLISHED, "Unpaywall"),
        ),
    )

    def fetch(url, *args, **kwargs):
        order.append(url)
        if url == PUBLISHED:
            return Response(url, status=403)
        return Response(url, body=pdf_bytes())

    monkeypatch.setattr(oa, "fetch", fetch)
    monkeypatch.setattr(
        sources,
        "_plan",
        lambda *args: [("Publisher", lambda *a: order.append("Publisher") or None)],
    )
    monkeypatch.setattr(
        sources,
        "call_legacy",
        lambda label, fn, *args: (fn(), oa.NotFound())[1],
    )
    result = sources.LegacySources().acquire(
        DOI, AcquisitionRequest(identifiers=[DOI]), tmp_path, {}
    )
    assert isinstance(result, SourceSuccess)
    assert order == [PUBLISHED, "Publisher", PREPRINT]
    assert result.url == PREPRINT
    assert result.warnings == [oa.PREPRINT_WARNING]
    assert [a.source for a in result.attempts][-1] == "Unpaywall"


def test_failure_keeps_held_preprints_for_the_caller(monkeypatch, tmp_path):
    held = Candidate(PREPRINT, "Unpaywall", preprint=True)
    monkeypatch.setattr(oa, "discover", _discover(held))
    monkeypatch.setattr(
        oa, "fetch", lambda *a, **k: pytest.fail("preprint fetched too early")
    )
    result = oa.acquire_oa(DOI, tmp_path, {})
    assert isinstance(result, SourceFailure)
    assert result.preprints == [held]


def stamped_pdf(text="The copyright holder for this preprint (which was not certified"):
    import pymupdf

    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "A study of something")
        page.insert_text((72, 100), text)
        return document.tobytes()


def test_first_page_stamp_marks_a_preprint(tmp_path):
    from academic_source.infrastructure.documents import looks_like_preprint

    stamped = tmp_path / "stamped.pdf"
    stamped.write_bytes(stamped_pdf())
    arxiv = tmp_path / "arxiv.pdf"
    arxiv.write_bytes(stamped_pdf("arXiv:1706.03762v7  [cs.CL]  2 Aug 2023"))
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(pdf_bytes())
    assert looks_like_preprint(stamped) and looks_like_preprint(arxiv)
    assert not looks_like_preprint(plain)


def test_unlabelled_preprint_download_waits_for_a_published_copy(monkeypatch, tmp_path):
    content = "https://content.openalex.org/works/W1.pdf"
    monkeypatch.setattr(
        oa,
        "discover",
        _discover(Candidate(content, "OpenAlexContent"), Candidate(PUBLISHED, "PMC")),
    )
    monkeypatch.setattr(
        oa,
        "fetch",
        lambda url, *a, **k: Response(
            url, body=stamped_pdf() if url == content else pdf_bytes()
        ),
    )
    result = oa.acquire_oa(DOI, tmp_path, {})
    assert isinstance(result, SourceSuccess) and result.url == PUBLISHED
    assert [a.status for a in result.attempts][-2:] == ["preprint", "succeeded"]


def test_held_preprint_download_is_used_last_with_a_warning(monkeypatch, tmp_path):
    content = "https://content.openalex.org/works/W1.pdf"
    order = []
    monkeypatch.setattr(
        oa, "discover", _discover(Candidate(content, "OpenAlexContent"))
    )

    def fetch(url, *a, **k):
        order.append(url)
        return Response(url, body=stamped_pdf())

    monkeypatch.setattr(oa, "fetch", fetch)
    monkeypatch.setattr(
        sources,
        "_plan",
        lambda *args: [("Publisher", lambda *a: order.append("Publisher") or None)],
    )
    monkeypatch.setattr(
        sources, "call_legacy", lambda label, fn, *args: (fn(), oa.NotFound())[1]
    )
    result = sources.LegacySources().acquire(
        DOI, AcquisitionRequest(identifiers=[DOI]), tmp_path, {}
    )
    assert isinstance(result, SourceSuccess) and result.url == content
    assert order == [content, "Publisher"]
    assert result.warnings == [oa.PREPRINT_WARNING]


def test_preprint_doi_tries_the_preprint_first(monkeypatch, tmp_path):
    visited = []
    monkeypatch.setattr(
        oa,
        "discover",
        _discover(
            Candidate(PUBLISHED, "Unpaywall"),
            Candidate(PREPRINT, "Unpaywall", preprint=True),
        ),
    )

    def fetch(url, *a, **k):
        visited.append(url)
        return Response(url, body=stamped_pdf())

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa("10.1101/2022.04.19.488821", tmp_path, {})
    assert isinstance(result, SourceSuccess) and result.url == PREPRINT
    assert visited == [PREPRINT] and not result.warnings
