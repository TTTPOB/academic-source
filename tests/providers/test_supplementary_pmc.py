import json
from io import BytesIO
from pathlib import Path
from threading import Event
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import pytest
from requests import ConnectionError, Response, Session

from scansci_pdf.pmc_cloud import BUCKET_URL, IDCONV_URL, CloudFile
from scansci_pdf.supplementary import SupplementaryItem, fetch_supplementary
from scansci_pdf.supplementary_pmc import fetch_pmc_supplementary

FIXTURES = Path(__file__).parent / "fixtures" / "supplementary"
DOI = "10.1093/nar/gkt922"
PMCID = "PMC3902920"
PREFIX = f"{PMCID}.2"
LOOKUP = f"{IDCONV_URL}?" + urlencode(
    {"ids": DOI, "format": "json", "tool": "academic-source"}
)
LISTING = f"{BUCKET_URL}/?list-type=2&prefix={PMCID}."
XML = f"{PREFIX}.xml"
PDF = "supp_gkt922_nar-01762-r-2013-File006.pdf"
HTML = "supp_42_2_1341__index.html"
PDF_BODY = b"%PDF-1.7\n"
HTML_BODY = b"<html>index</html>"


def url_for(name):
    return CloudFile(f"{PREFIX}/{name}", 0).url


def listing(entries):
    members = "".join(
        f"<Contents><Key>{escape(PREFIX + '/' + name)}</Key><Size>{size}</Size></Contents>"
        for name, size in entries.items()
    )
    return (
        f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"{members}</ListBucketResult>"
    ).encode()


class Body(BytesIO):
    def release_conn(self):
        self.close()


def response(url, body, *, status=200):
    res = Response()
    res.url = url
    res.status_code = status
    res.raw = Body(body)
    return res


def fake_fetch(
    files=None, *, xml=None, records=None, sizes=None, status=None, on_read=None
):
    xml = (FIXTURES / "pmc3902920-oxford-jats.xml").read_bytes() if xml is None else xml
    records = [{"doi": DOI, "pmcid": PMCID}] if records is None else records
    bodies = {XML: xml, **(files or {})}
    entries = {name: len(body) for name, body in bodies.items()} | (sizes or {})
    routes = {
        LOOKUP: json.dumps({"records": records}).encode(),
        LISTING: listing(entries),
        **{url_for(name): body for name, body in bodies.items()},
    }
    calls = {}

    def fetch(url):
        outcome = (status or {}).get(url, 200)
        if isinstance(outcome, Exception):
            calls[url] = None
            raise outcome
        assert url not in calls
        res = response(url, routes[url], status=outcome)
        if on_read:
            on_read(res)
        calls[url] = res.raw
        return res

    return fetch, calls


def run_route(tmp_path, files=None, *, limits=None, **options):
    fetch, calls = fake_fetch(files, **options)
    result = fetch_pmc_supplementary(
        DOI,
        tmp_path,
        fetch,
        **({"max_file_bytes": 16_000, "max_total_bytes": 32_000} | (limits or {})),
    )
    assert all(raw is None or raw.closed for raw in calls.values())
    return result, list(calls)


def test_matches_jats_and_retains_unmatched_and_missing_files(tmp_path):
    path = f"nested/{PDF.upper()}"
    result, _ = run_route(tmp_path, {path: PDF_BODY, "notes.txt": b"notes"})

    assert result.source == result.route == "PMC"
    assert result.pmcid == PMCID and result.pages_read == 1
    assert [entry.page_url for entry in result.saved] == [
        f"https://pmc.ncbi.nlm.nih.gov/articles/{PMCID}/"
    ] * 2
    assert [entry.path for entry in result.saved] == [
        tmp_path / path,
        tmp_path / "notes.txt",
    ]
    assert [entry.item for entry in result.saved] == [
        SupplementaryItem(
            url_for(path), "Supplementary Data", "Supplementary Data", PDF
        ),
        SupplementaryItem(url_for("notes.txt"), "notes.txt", filename="notes.txt"),
    ]
    assert result.saved[0].path.read_bytes() == PDF_BODY
    [(missing, reason)] = [
        (entry.item.filename, entry.reason) for entry in result.unsaved
    ]
    assert (missing, reason) == (HTML, "file is absent from PMC Cloud")


def test_exact_match_wins_and_invalid_pdf_is_not_saved(tmp_path):
    result, _ = run_route(
        tmp_path,
        {
            f"other/{PDF.upper()}": PDF_BODY,
            PDF: PDF_BODY,
            "fake.pdf": b"<!doctype html><html>not a PDF</html>",
            HTML: HTML_BODY,
        },
    )
    assert [(entry.path.name, entry.item.label) for entry in result.saved] == [
        (HTML, "Supplementary Data"),
        (PDF, "Supplementary Data"),
        (PDF.upper(), PDF.upper()),
    ]
    assert [(entry.item.filename, entry.reason) for entry in result.unsaved] == [
        ("fake.pdf", "not a file (HTML page)"),
    ]
    assert not (tmp_path / "fake.pdf").exists()


def test_ambiguous_bucket_basenames_are_not_assigned_jats_captions(tmp_path):
    names = [f"a/{PDF}", f"b/{PDF}"]
    result, _ = run_route(tmp_path, {name: PDF_BODY for name in names})
    assert [entry.path for entry in result.saved] == [tmp_path / name for name in names]
    assert [entry.item.label for entry in result.saved] == [PDF, PDF]
    assert [entry.item.filename for entry in result.unsaved] == [HTML, PDF]


def test_rejects_paths_outside_output_before_fetching(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "link").symlink_to(tmp_path, target_is_directory=True)
    absolute = str(tmp_path / "absolute.txt")
    unsafe = ["../traversal.txt", absolute, "link/symlink.txt"]
    result, calls = run_route(
        out,
        {**{name: b"escape" for name in unsafe}, "directory/": b"", PDF: PDF_BODY},
    )
    assert [entry.path.name for entry in result.saved] == [PDF]
    assert sum("unsafe" in entry.reason for entry in result.unsaved) == 3
    assert calls == [LOOKUP, LISTING, url_for(XML), url_for(PDF)]
    assert not (tmp_path / "traversal.txt").exists()
    assert not (tmp_path / "absolute.txt").exists()
    assert not (tmp_path / "symlink.txt").exists()
    assert not (out / "directory").exists()


def test_keeps_unmatched_files_even_without_jats_supplementary_material(tmp_path):
    files = {f"table-{i:02}.txt": b"data" for i in range(48)}
    result, _ = run_route(tmp_path, files, xml=b"<article><body/></article>")
    assert [entry.item.filename for entry in result.saved] == list(files)
    assert not result.unsaved


def test_skips_large_object_and_keeps_smaller_neighbours(tmp_path):
    result, calls = run_route(
        tmp_path,
        {PDF: PDF_BODY + b"a" * 32, HTML: HTML_BODY, "notes.txt": b"notes"},
        limits={"max_file_bytes": 24},
    )
    assert [entry.item.filename for entry in result.saved] == [HTML, "notes.txt"]
    (skipped,) = result.unsaved
    assert skipped.item.filename == PDF and skipped.item.label == "Supplementary Data"
    assert skipped.item.url == url_for(PDF)
    assert skipped.reason == "too large (41 bytes; limit 24 bytes)"
    assert url_for(PDF) not in calls


def test_total_limit_uses_listing_sizes_in_jats_order(tmp_path):
    result, calls = run_route(
        tmp_path,
        {"tiny.txt": b"tiny", PDF: PDF_BODY, HTML: HTML_BODY},
        limits={"max_total_bytes": 24},
    )
    assert [entry.item.filename for entry in result.saved] == [HTML]
    assert [(entry.item.filename, entry.reason) for entry in result.unsaved] == [
        (PDF, "over the per-paper size limit (24 bytes)"),
        ("tiny.txt", "over the per-paper size limit (24 bytes)"),
    ]
    assert calls == [LOOKUP, LISTING, url_for(XML), url_for(HTML)]


def test_streams_are_bounded_and_partial_files_are_removed(tmp_path):
    result, _ = run_route(
        tmp_path,
        {HTML: HTML_BODY, PDF: PDF_BODY, "empty.txt": b""},
        sizes={HTML: len(HTML_BODY) + 1, PDF: len(PDF_BODY) - 1},
    )
    assert not result.saved and not list(tmp_path.iterdir())
    assert [(entry.item.filename, entry.reason) for entry in result.unsaved] == [
        (HTML, "truncated transfer"),
        (PDF, "exceeded PMC listing size"),
        ("empty.txt", "not a file (empty response)"),
    ]


@pytest.mark.parametrize("records", [[], [{"doi": DOI}]])
def test_without_matching_pmcid_does_not_list_bucket(tmp_path, records):
    result, calls = run_route(tmp_path, records=records)
    assert calls == [LOOKUP]
    assert not result.pmcid and not result.saved and not result.unsaved
    assert not result.route_warnings


def test_author_manuscript_without_attachments_does_not_fetch_xml(tmp_path):
    result, calls = run_route(
        tmp_path, {f"{PREFIX}.txt": b"text", f"{PREFIX}.json": b"{}"}
    )
    assert calls == [LOOKUP, LISTING]
    assert result.pmcid == PMCID and result.pages_read == 0
    assert not result.saved and not result.unsaved and not result.route_warnings


@pytest.mark.parametrize("stage", [LOOKUP, LISTING])
@pytest.mark.parametrize("outcome", [503, ConnectionError("connection lost")])
def test_lookup_and_listing_failures_leave_route_warning_without_retries(
    tmp_path, stage, outcome
):
    result, calls = run_route(tmp_path, status={stage: outcome})
    if stage == LOOKUP:
        assert calls == [LOOKUP]
        assert not result.pmcid
        assert result.route_warnings == ["PMC DOI lookup failed"]
    else:
        assert calls == [LOOKUP, LISTING]
        assert result.pmcid == PMCID
        assert result.route_warnings == ["PMC Cloud file listing unavailable"]
    assert not result.saved and not result.unsaved


def test_unreadable_jats_does_not_guess_which_files_are_supplementary(tmp_path):
    result, calls = run_route(tmp_path, {PDF: PDF_BODY}, xml=b"not JATS")
    assert calls == [LOOKUP, LISTING, url_for(XML)]
    assert result.pages_unread == 1 and not result.saved
    assert result.route_warnings == ["PMC full-text XML unavailable"]


def test_retains_item_metadata_on_download_failure(tmp_path):
    result, _ = run_route(
        tmp_path, {PDF: PDF_BODY, HTML: HTML_BODY}, status={url_for(PDF): 503}
    )
    assert [entry.path.name for entry in result.saved] == [HTML]
    (skipped,) = result.unsaved
    assert skipped.reason == "HTTP 503"
    assert skipped.item == SupplementaryItem(
        url_for(PDF), "Supplementary Data", "Supplementary Data", PDF
    )


def test_drops_the_articles_own_figures_and_formulas(tmp_path):
    xml = b"""<article xmlns:xlink="http://www.w3.org/1999/xlink"><body>
      <fig id="fig1"><graphic xlink:href="gr1_lrg"/></fig>
      <p><inline-graphic xlink:href="um1.jpg"/></p>
      <supplementary-material id="mmc1" xlink:href="mmc1.xlsx">
        <label>Table S1</label>
      </supplementary-material>
      <supplementary-material id="mmc2">
        <label>Figure S9</label><graphic xlink:href="figs9"/>
        <media xlink:href="figs9.jpg"/>
      </supplementary-material>
    </body></article>"""
    result, calls = run_route(
        tmp_path,
        {
            "mmc1.xlsx": b"PK\x03\x04 workbook",
            "figs9.jpg": b"\xff\xd8\xff supplementary figure",
            "gr1_lrg.jpg": b"\xff\xd8\xff figure",
            "gr1_lrg.gif": b"GIF89a figure",
            "um1.jpg": b"\xff\xd8\xff formula",
            "readme.txt": b"unlisted but kept",
        },
        xml=xml,
    )
    expected = ["mmc1.xlsx", "figs9.jpg", "readme.txt"]
    assert [entry.path.name for entry in result.saved] == expected
    assert set(calls[3:]) == {url_for(name) for name in expected}
    assert not result.unsaved


def test_parallel_streaming_through_upstream_gate_preserves_jats_order(
    tmp_path, monkeypatch
):
    later_closed = Event()

    def on_read(res):
        if res.url == url_for(HTML):
            original_read = res.raw.read

            def read(size):
                assert later_closed.wait(5), (
                    "the later attachment did not finish in parallel"
                )
                return original_read(size)

            res.raw.read = read
        elif res.url == url_for(PDF):
            original_close = res.raw.close

            def close():
                original_close()
                later_closed.set()

            res.raw.close = close

    fetch, calls = fake_fetch({PDF: PDF_BODY, HTML: HTML_BODY}, on_read=on_read)

    def request(self, method, url, **options):
        assert method == "GET" and options["stream"] is True
        assert options["proxies"] == {"http": "http://proxy", "https": "http://proxy"}
        return fetch(url)

    monkeypatch.setattr(Session, "request", request)
    result = fetch_supplementary(
        DOI, tmp_path, {"_parallel_http": True, "network_proxy": "http://proxy"}
    )
    assert [entry.path.name for entry in result.saved] == [HTML, PDF]
    assert all(raw.closed for raw in calls.values())
