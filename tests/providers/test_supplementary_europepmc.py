from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from requests import Response

from scansci_pdf.supplementary import SupplementaryItem
from scansci_pdf.supplementary_europepmc import fetch_europepmc_supplementary

FIXTURES = Path(__file__).parent / "fixtures" / "supplementary"
DOI = "10.1093/nar/gkt922"
BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3902920"
XML_URL = f"{BASE}/fullTextXML"
ZIP_URL = f"{BASE}/supplementaryFiles"
PDF = "supp_gkt922_nar-01762-r-2013-File006.pdf"
HTML = "supp_42_2_1341__index.html"


def make_zip(members):
    data = BytesIO()
    with ZipFile(data, "w", ZIP_DEFLATED) as bundle:
        for name, body in members.items():
            bundle.writestr(name, body)
    return data.getvalue()


def run_route(
    tmp_path, *, search=None, xml=None, bundle=None, zip_status=200, **limits
):
    search = (
        (FIXTURES / "europepmc-search-oxford.json").read_bytes()
        if search is None
        else search
    )
    xml = (FIXTURES / "pmc3902920-oxford-jats.xml").read_bytes() if xml is None else xml
    calls = []

    def fetch(url):
        calls.append(url)
        response = Response()
        response.url = url
        response.status_code = 200
        if urlsplit(url).path.endswith("/search"):
            assert parse_qs(urlsplit(url).query) == {
                "query": [f'DOI:"{DOI}"'],
                "format": ["json"],
                "resultType": ["lite"],
            }
            response._content = search
            response._content_consumed = True
        elif url == XML_URL:
            response._content = xml
            response._content_consumed = True
        else:
            assert url == ZIP_URL
            assert bundle is not None or zip_status != 200
            response.status_code = zip_status
            response.raw = BytesIO(bundle or b"")
        return response

    defaults = {"max_file_bytes": 16_000, "max_total_bytes": 32_000}
    return fetch_europepmc_supplementary(
        DOI, tmp_path, fetch, **(defaults | limits)
    ), calls


def test_europepmc_matches_jats_and_retains_unmatched_and_missing_files(tmp_path):
    pdf_path = f"nested/{PDF.upper()}"
    result, _ = run_route(
        tmp_path, bundle=make_zip({pdf_path: b"%PDF-1.7\n", "notes.txt": b"notes"})
    )

    assert result.pmcid == "PMC3902920"
    assert result.pages_read == 1
    assert [entry.page_url for entry in result.saved] == [XML_URL, XML_URL]
    assert [entry.path for entry in result.saved] == [
        tmp_path / pdf_path,
        tmp_path / "notes.txt",
    ]
    assert [entry.item for entry in result.saved] == [
        SupplementaryItem(ZIP_URL, "Supplementary Data", "Supplementary Data", PDF),
        SupplementaryItem(ZIP_URL, "notes.txt", filename="notes.txt"),
    ]
    assert result.saved[0].path.read_bytes() == b"%PDF-1.7\n"
    assert [(item.item.filename, item.reason) for item in result.unsaved] == [
        (HTML, "file is absent from the Europe PMC ZIP"),
    ]


def test_europepmc_exact_match_wins_and_invalid_pdf_is_not_saved(tmp_path):
    result, _ = run_route(
        tmp_path,
        bundle=make_zip(
            {
                f"other/{PDF.upper()}": b"%PDF-1.7\n",
                PDF: b"%PDF-1.7\n",
                "fake.pdf": b"<!doctype html><html>not a PDF</html>",
                HTML: b"<!doctype html><html>index</html>",
            }
        ),
    )
    assert [(entry.path.name, entry.item.label) for entry in result.saved] == [
        (HTML, "Supplementary Data"),
        (PDF, "Supplementary Data"),
        (PDF.upper(), PDF.upper()),
    ]
    assert [entry.item.filename for entry in result.unsaved] == ["fake.pdf"]
    assert not (tmp_path / "fake.pdf").exists()


def test_europepmc_rejects_zip_paths_outside_output(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "link").symlink_to(tmp_path, target_is_directory=True)
    absolute = str(tmp_path / "absolute.txt")
    result, _ = run_route(
        out,
        bundle=make_zip(
            {
                "../traversal.txt": b"escape",
                absolute: b"escape",
                "link/symlink.txt": b"escape",
                "directory/": b"",
                PDF: b"%PDF-1.7\n",
            }
        ),
    )
    assert [entry.path.name for entry in result.saved] == [PDF]
    assert sum("unsafe" in entry.reason for entry in result.unsaved) == 3
    assert not (tmp_path / "traversal.txt").exists()
    assert not (tmp_path / "absolute.txt").exists()
    assert not (tmp_path / "symlink.txt").exists()
    assert not (out / "directory").exists()


def test_europepmc_keeps_all_small_files(tmp_path):
    files = {f"table-{i:02}.txt": b"data" for i in range(48)}
    bundle = make_zip(
        {**files, "directory/": b"", PDF: b"%PDF-1.7\n", HTML: b"<html>x</html>"}
    )
    result, _ = run_route(tmp_path, bundle=bundle)
    assert [entry.item.filename for entry in result.saved] == [HTML, PDF, *files]
    assert not result.unsaved


def test_europepmc_limits_compressed_bytes(tmp_path):
    result, _ = run_route(
        tmp_path,
        bundle=make_zip({PDF: b"%PDF-1.7\n", HTML: b"<html>x</html>"}),
        max_total_bytes=100,
    )
    assert not result.saved
    assert all("compressed size limit" in entry.reason for entry in result.unsaved)
    assert not list(tmp_path.iterdir())


def test_europepmc_skips_large_member_and_keeps_smaller_neighbours(tmp_path):
    bundle = make_zip(
        {PDF: b"%PDF-1.7\n" + b"a" * 32, HTML: b"<html>x</html>", "notes.txt": b"notes"}
    )
    result, _ = run_route(tmp_path, bundle=bundle, max_file_bytes=16)
    assert [entry.item.filename for entry in result.saved] == [HTML, "notes.txt"]
    (skipped,) = result.unsaved
    assert skipped.item.filename == PDF and skipped.item.label == "Supplementary Data"
    assert skipped.item.url == ZIP_URL
    assert skipped.reason == "too large (41 bytes; limit 16 bytes)"


def test_europepmc_caps_total_uncompressed_bytes(tmp_path):
    pdf = b"%PDF-1.7\n" + b"0" * 511
    html = b"<html>" + b"0" * 511 + b"</html>"
    bundle = make_zip({"tiny.txt": b"tiny", PDF: pdf, HTML: html})
    assert len(bundle) < 650
    result, _ = run_route(tmp_path, bundle=bundle, max_total_bytes=650)
    assert [entry.item.filename for entry in result.saved] == [HTML]
    assert (tmp_path / HTML).read_bytes() == html
    assert [(entry.item.filename, entry.reason) for entry in result.unsaved] == [
        (PDF, "over the per-paper size limit (650 bytes)"),
        ("tiny.txt", "over the per-paper size limit (650 bytes)"),
    ]


@pytest.mark.parametrize("results", [b"[]", b'[{"doi":"10.1093/nar/gkt922"}]'])
def test_europepmc_without_matching_pmcid_does_not_fetch_xml(tmp_path, results):
    result, calls = run_route(
        tmp_path, search=b'{"resultList":{"result":' + results + b"}}"
    )
    assert len(calls) == 1
    assert not result.pmcid
    assert not result.saved and not result.unsaved


def test_europepmc_without_supplementary_material_does_not_fetch_zip(tmp_path):
    result, calls = run_route(tmp_path, xml=b"<article><body/></article>")
    assert calls[-1] == XML_URL
    assert result.pages_read == 1
    assert not result.saved and not result.unsaved


def test_europepmc_retains_item_metadata_on_zip_failure(tmp_path):
    result, calls = run_route(tmp_path, zip_status=503)
    assert calls[-1] == ZIP_URL
    assert not result.saved
    assert [entry.item.filename for entry in result.unsaved] == [HTML, PDF]
    assert all("could not fetch" in entry.reason for entry in result.unsaved)


def test_transient_server_errors_are_retried(monkeypatch):
    from requests import ConnectionError as RequestsConnectionError

    from scansci_pdf import supplementary_europepmc as route

    monkeypatch.setattr(route, "_RETRY_DELAY", 0)
    outcomes = [503, RequestsConnectionError("stalled"), 200]

    def fetch(url):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        response = Response()
        response.status_code = outcome
        response.raw = BytesIO()
        return response

    assert route._retrying(fetch)("https://example.org").status_code == 200
    assert outcomes == []

    outcomes[:] = [503, 503, 503]
    assert route._retrying(fetch)("https://example.org").status_code == 503
    assert outcomes == []
