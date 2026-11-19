"""Offline exercises of generic supplementary discovery and download."""

from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import get_ident
from types import SimpleNamespace

import pytest
from requests import RequestException
from requests import Response as HTTPResponse
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from scansci_pdf import browser_engine
from scansci_pdf import supplementary as supp
from scansci_pdf.publisher_strategies import StrategyRegistry
from scansci_pdf.publisher_strategies.base import BasePublisherStrategy

START = "https://publisher.example/start"
ARTICLE = "https://publisher.example/final/article"


class Response:
    def __init__(self, url, *, status=200, text="", headers=None, chunks=None):
        self.url = url
        self.status_code = status
        self.text = text
        self.headers = CaseInsensitiveDict(headers or {})
        self.chunks = (
            chunks
            if chunks is not None
            else (
                {
                    ".pdf": b"%PDF-1.7",
                    ".zip": b"PK file",
                }.get(url[-4:], b"file"),
            )
        )
        self.closed = False

    @property
    def is_redirect(self):
        return (
            self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, *, chunk_size):
        yield from self.chunks


@pytest.fixture(autouse=True)
def publisher(monkeypatch):
    strategy = BasePublisherStrategy()
    strategy.article_url_template = START
    monkeypatch.setattr(StrategyRegistry, "get_for_doi", lambda doi: strategy)
    monkeypatch.setattr(supp, "_fetch_europepmc", lambda *_: supp.SupplementaryResult())
    return strategy


def test_generic_keeps_captions_and_uses_the_final_page_url(tmp_path, monkeypatch):
    text = """
    <ul><li><a href=movie_supp_1.zip>Movie <b>S1</b></a><p>A long caption.
We retained the new line &amp; punctuation!</p></li></ul>
    <figure><a href="fig_supp_2.pdf">Figure S2</a><figcaption>A separate caption.</figcaption></figure>
    <table><tr><td><a href="/attachment/table">Table S3</a></td><td>A table caption.</td></tr></table>
    <p><a href="data_supp_4.csv">Data S4</a> Paragraph caption.</p>
    <a href="/supplements/main.pdf">Main PDF</a><a href="#MOESM1">Anchor</a>
    <a href="movie_supp_1.zip#duplicate">Duplicate</a>
    """
    files = [
        ("/final/movie_supp_1.zip", {}),
        ("/final/fig_supp_2.pdf", {"Content-Type": "application/pdf"}),
        (
            "/attachment/table",
            {"Content-Disposition": "attachment; filename*=UTF-8''Table%20S3.xlsx"},
        ),
        ("/final/data_supp_4.csv", {}),
    ]
    responses = {START: Response(ARTICLE, text=text)}
    responses.update(
        {
            "https://publisher.example" + path: Response(
                "https://publisher.example" + path,
                headers=headers,
                **(
                    {"chunks": (b"PK workbook",)} if path == "/attachment/table" else {}
                ),
            )
            for path, headers in files
        }
    )

    def get(self, url, **options):
        assert options["stream"] is True
        return responses[url]

    monkeypatch.setattr(supp.requests.Session, "get", get)
    result = supp.fetch_supplementary(
        "10.1/paper", tmp_path, run_browser=lambda *_: pytest.fail("HTTP succeeded")
    )
    assert [saved.item.label for saved in result.saved] == [
        "Movie S1",
        "Figure S2",
        "Table S3",
        "Data S4",
    ]
    assert [saved.item.description for saved in result.saved] == [
        "A long caption.\nWe retained the new line & punctuation!",
        "A separate caption.",
        "A table caption.",
        "Paragraph caption.",
    ]
    assert [saved.item.filename for saved in result.saved] == [
        "movie_supp_1.zip",
        "fig_supp_2.pdf",
        "Table S3.xlsx",
        "data_supp_4.csv",
    ]
    assert result.saved[2].path.name == "10.1_paper_SI3.xlsx"
    assert all(saved.page_url == ARTICLE for saved in result.saved)
    assert all(response.closed for response in responses.values())
    assert result.pages_read == 1 and not result.unsaved


def test_hooks_read_ordered_pages_respect_empty_and_deduplicate(
    tmp_path, monkeypatch, publisher
):
    alt_url = START + "/supplementary"
    actual_url = "https://publisher.example/not-matched-by-generic.mp4"
    seen = []
    publisher.supplementary_pages = lambda doi: [alt_url, START]

    def items(html, page_url):
        seen.append((html, page_url))
        if page_url == ARTICLE:
            return []
        item = supp.SupplementaryItem(
            actual_url, label="Movie S1", description="Publisher caption"
        )
        return [item, item]

    publisher.supplementary_items = items
    routes = {
        alt_url: Response(alt_url, text="<p>Alternate page</p>"),
        START: Response(ARTICLE, text='<a href="supp_1.pdf">Do not use fallback</a>'),
        actual_url: Response(actual_url),
    }
    monkeypatch.setattr(
        supp.requests.Session, "get", lambda self, url, **kwargs: routes[url]
    )
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    assert seen == [(routes[alt_url].text, alt_url), (routes[START].text, ARTICLE)]
    assert result.pages_read == 2 and not result.unsaved
    assert [(file.item.description, file.path.suffix) for file in result.saved] == [
        ("Publisher caption", ".mp4")
    ]


def test_versioned_tab_is_read_after_redirect(tmp_path, monkeypatch, publisher):
    tab = START + ".supplementary-material"
    versioned = ARTICLE + ".supplementary-material"
    file = "https://publisher.example/final/supp_1.pdf"
    publisher.supplementary_pages = lambda doi: [tab]
    routes = {
        tab: Response(
            ARTICLE,
            text='<a href="article.supplementary-material">Supplementary material</a>',
        ),
        versioned: Response(versioned, text='<a href="supp_1.pdf">Appendix</a>'),
        file: Response(file),
    }
    monkeypatch.setattr(
        supp.requests.Session, "get", lambda self, url, **kwargs: routes[url]
    )
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    assert result.pages_read == 2 and result.saved[0].page_url == versioned


@pytest.mark.parametrize("pmc_saved", [False, True])
def test_europepmc_runs_first_and_skips_publisher_when_it_delivers(
    tmp_path, monkeypatch, pmc_saved
):
    calls = []
    xml = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC123/fullTextXML"
    item = supp.SupplementaryItem(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC123/supplementaryFiles",
        "Data S1",
    )
    publisher_file = "https://publisher.example/supp_publisher.zip"
    responses = {
        START: Response(
            START, text='<a href="supp_publisher.zip">Publisher supplement</a>'
        ),
        publisher_file: Response(publisher_file),
    }

    def get(self, url, **kwargs):
        calls.append(url)
        return responses[url]

    def fetch_pmc(doi, out, session, config, timeout):
        calls.append("Europe PMC")
        if not pmc_saved:
            return supp.SupplementaryResult(
                pages_read=1, unsaved=[supp.UnsavedSupplementary(item, "HTTP 503")]
            )
        path = out / "pmc.zip"
        path.write_bytes(b"PK file")
        return supp.SupplementaryResult(
            pages_read=1,
            saved=[supp.SavedSupplementary(path, item, xml)],
            source="Europe PMC",
            route="Europe PMC",
            pmcid="PMC123",
        )

    monkeypatch.setattr(supp.requests.Session, "get", get)
    monkeypatch.setattr(supp, "_fetch_europepmc", fetch_pmc)
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    (saved,) = result.saved
    if pmc_saved:
        assert calls == ["Europe PMC"]
        assert (
            result.route == result.source == "Europe PMC" and result.pmcid == "PMC123"
        )
        assert saved.page_url == xml
    else:
        assert calls == ["Europe PMC", START, publisher_file]
        assert saved.item.label == "Publisher supplement"
        assert result.unsaved == [supp.UnsavedSupplementary(item, "HTTP 503")]
        assert result.route_warnings == [
            "Europe PMC did not provide supplementary files; tried the publisher route"
        ]


def test_file_type_validation_for_publisher_downloads(tmp_path, monkeypatch, publisher):
    items = [
        ("table.xlsx", b"PK workbook", "text/html", None, ""),
        ("movie.mp4", b"movie bytes", "video/mp4", None, ""),
        ("archive.zip", b"PK archive", "application/octet-stream", None, ""),
        (
            "appendix.pdf",
            b"<!DOCTYPE html><html>login</html>",
            "application/pdf",
            None,
            "not a file (HTML page)",
        ),
        (
            "challenge.xlsx",
            b"<html>Just a moment...</html>",
            "text/html",
            None,
            "not a file (HTML page)",
        ),
        ("index.html", b"<!DOCTYPE html><html>Index</html>", "text/html", None, ""),
        ("empty.txt", b"", "text/plain", None, "not a file (empty response)"),
        ("truncated.csv", b"abc", "text/csv", 10, "truncated transfer (3 of 10 bytes)"),
        (
            "viewer.pdf",
            b"viewer stub",
            "application/pdf",
            None,
            "returned content is not a PDF file",
        ),
    ]
    publisher.supplementary_items = lambda html, url: [
        supp.SupplementaryItem("https://publisher.example/" + name, label=name)
        for name, *_ in items
    ]
    routes = {START: Response(START, text="<p>Attachments</p>")}
    for name, body, ctype, size, _ in items:
        url = "https://publisher.example/" + name
        headers = {"Content-Type": ctype}
        if size is not None:
            headers["Content-Length"] = str(size)
        routes[url] = Response(url, chunks=(body,), headers=headers)
    monkeypatch.setattr(
        supp.requests.Session, "get", lambda self, url, **kwargs: routes[url]
    )
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    assert {saved.item.label: saved.path.read_bytes() for saved in result.saved} == {
        name: body for name, body, _, _, reason in items if not reason
    }
    assert {item.item.label: item.reason for item in result.unsaved} == {
        name: reason for name, _, _, _, reason in items if reason
    }
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("parallel", [False, True])
def test_extensionless_publisher_download_uses_get_and_follows_cdn_redirect(
    tmp_path, monkeypatch, parallel
):
    item = "https://publisher.example/article/file?type=supplementary-file"
    cdn = "https://objects.example/item"
    payload = b"II*\x00offline TIFF"
    calls = []

    def send(_adapter, request, *, stream, **kwargs):
        calls.append((request.method, request.url))
        assert stream
        response = HTTPResponse()
        response.request = request
        response.url = request.url
        response.status_code = 200
        if request.url == START:
            response.raw = BytesIO(f'<a href="{item}">Figure S1</a>'.encode())
        elif request.url == item:
            response.status_code = 302
            response.headers["Location"] = cdn
            response.raw = BytesIO(b"")
        else:
            assert request.url == cdn
            response.headers.update(
                {"Content-Type": "image/tiff", "Content-Length": str(len(payload))}
            )
            response.raw = BytesIO(payload)
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    result = supp.fetch_supplementary(
        "10.1/paper", tmp_path, {"_parallel_http": parallel}
    )
    assert calls == [("GET", url) for url in (START, item, cdn)]
    (saved,) = result.saved
    assert saved.path.suffix == ".tiff"
    assert saved.path.read_bytes() == payload
    assert saved.item.url == item


@pytest.mark.parametrize(
    ("ctype", "filename", "expected", "payload"),
    [
        ("image/png", "", ".png", b"image"),
        ("image/jpeg", "", ".jpg", b"image"),
        ("image/gif", "", ".gif", b"image"),
        ("image/svg+xml", "", ".svg", b"<svg/>"),
        ("video/mp4", "", ".mp4", b"video"),
        ("video/quicktime", "", ".mov", b"video"),
        ("video/x-msvideo", "", ".avi", b"video"),
        ("video/webm", "", ".webm", b"video"),
        ("audio/mpeg", "", ".mp3", b"sound"),
        ("application/pdf", "", ".pdf", b"%PDF-1.7"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "",
            ".docx",
            b"PK",
        ),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "",
            ".xlsx",
            b"PK",
        ),
        (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "",
            ".pptx",
            b"PK",
        ),
        ("text/csv", "", ".csv", b"a,b"),
        ("text/tab-separated-values", "", ".tsv", b"a\tb"),
        ("text/plain", "", ".txt", b"text"),
        ("application/xml", "", ".xml", b"<xml/>"),
        ("application/json", "", ".json", b"{}"),
        ("application/zip", "", ".zip", b"PK"),
        ("application/gzip", "", ".gz", b"\x1f\x8b"),
        ("application/x-tar", "", ".tar", b"tar"),
        ("application/example", "", ".bin", b"raw"),
        ("application/octet-stream", "table.xlsx", ".xlsx", b"PK workbook"),
    ],
)
def test_attachment_extension_prefers_filename_then_mime(
    tmp_path, monkeypatch, publisher, ctype, filename, expected, payload
):
    url = "https://publisher.example/download"
    publisher.supplementary_items = lambda *_: [
        supp.SupplementaryItem(url, filename=filename)
    ]
    routes = {
        START: Response(START, text="<p>Attachments</p>"),
        url: Response(url, headers={"Content-Type": ctype}, chunks=(payload,)),
    }
    monkeypatch.setattr(
        supp.requests.Session, "get", lambda self, url, **kwargs: routes[url]
    )
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    (saved,) = result.saved
    assert saved.path.suffix == expected
    assert saved.path.read_bytes() == payload


def test_download_limits_and_reason_for_every_unsaved_item(tmp_path, monkeypatch):
    assert supp.MAX_FILE_BYTES == 200_000_000
    page = "".join(f'<a href="supp_{i}.zip">Data {i}</a>' for i in range(56))
    requested = []

    def no_body():
        pytest.fail("An oversized or non-file body must not be read")
        yield b""

    def unchecked_length():
        yield b"a" * 5
        yield b"a" * 4
        pytest.fail("Stream must stop as soon as it passes the size limit")

    def interrupted():
        yield b"a"
        raise RequestException("no details for untrusted exceptions")

    def get(self, url, **kwargs):
        if url == START:
            return Response(ARTICLE, text=page)
        requested.append(url)
        return {
            "supp_0.zip": lambda: Response(
                url, headers={"Content-Length": "200000001"}, chunks=no_body()
            ),
            "supp_1.zip": lambda: Response(url, chunks=unchecked_length()),
            "supp_2.zip": lambda: Response(url, status=403, chunks=no_body()),
            "supp_3.zip": lambda: Response(
                url,
                headers={"Content-Type": "text/html; charset=utf-8"},
                chunks=(b"<html",),
            ),
            "supp_4.zip": lambda: Response(url, chunks=()),
            "supp_5.zip": lambda: Response(url, chunks=interrupted()),
            "supp_6.zip": lambda: Response(url, chunks=(b"PK" + b"a" * 6,)),
        }.get(url.rsplit("/", 1)[-1], lambda: Response(url))()

    monkeypatch.setattr(supp, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(supp.requests.Session, "get", get)
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    assert len(requested) == 56
    assert len(result.saved) == 50 and result.saved[0].path.stat().st_size == 8
    assert [item.reason for item in result.unsaved] == [
        "too large (200,000,001 bytes; limit 8 bytes)",
        "too large (at least 9 bytes; limit 8 bytes)",
        "HTTP 403",
        "not a file (HTML page)",
        "not a file (empty response)",
        "network error",
    ]
    assert not list(tmp_path.glob("*.part"))


def test_publisher_byte_budget_spans_pages_and_preserves_completed_files(
    tmp_path, monkeypatch, publisher
):
    assert supp.MAX_PAPER_BYTES == 600_000_000
    monkeypatch.setattr(supp, "MAX_PAPER_BYTES", 10)
    second = "https://publisher.example/other"
    publisher.supplementary_pages = lambda _: [START, second]
    publisher.supplementary_items = lambda _html, url: [
        supp.SupplementaryItem(f"https://publisher.example/{i}.txt", label=f"Data {i}")
        for i in ([0] if url == START else range(1, 5))
    ]
    calls = []

    def no_body():
        pytest.fail(
            "An advertised or streamed excess must not be read beyond the budget"
        )
        yield b""

    def over_budget():
        yield b"abc"
        yield b"de"
        yield from no_body()

    def get(self, url, **kwargs):
        calls.append(url)
        if url in {START, second}:
            return Response(url, text="<p>Supplements</p>")
        return {
            "0.txt": Response(url, chunks=(b"abcdef",)),
            "1.txt": Response(url, chunks=no_body(), headers={"Content-Length": "5"}),
            "2.txt": Response(url, chunks=over_budget()),
            "3.txt": Response(url, chunks=(b"abcd",)),
        }[url.rsplit("/", 1)[-1]]

    monkeypatch.setattr(supp.requests.Session, "get", get)
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    assert calls[-1].endswith(
        "3.txt"
    )  # No GET for later items once the budget is full.
    assert [entry.item.label for entry in result.saved] == ["Data 0", "Data 3"]
    assert sum(entry.path.stat().st_size for entry in result.saved) == 10
    assert [(entry.item.label, entry.reason) for entry in result.unsaved] == [
        (f"Data {i}", "over the per-paper size limit (10 bytes)") for i in (1, 2, 4)
    ]
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize(
    "browser_state", ["success", "navigation_error", "unavailable"]
)
def test_browser_fallback_opens_downloads_and_closes_on_same_worker(
    tmp_path, monkeypatch, browser_state
):
    timeline = []
    thread = get_ident()

    class Page:
        url = ARTICLE

        def __init__(self):
            self.context = SimpleNamespace(cookies=list)

        def goto(self, url, **options):
            assert url == START
            timeline.append(("go", get_ident()))
            if browser_state == "navigation_error":
                raise RuntimeError("navigation failed")
            return SimpleNamespace(ok=True, url=ARTICLE)

        def wait_for_timeout(self, _):
            pass

        def evaluate(self, expr):
            assert expr == "navigator.userAgent"
            return "offline-test-browser"

        def content(self):
            return '<a href="https://cdn.example/supp_1.pdf">Browser Supplement</a>'

        def close(self):
            timeline.append(("close", get_ident()))

    def page_for(_):
        timeline.append(("open", get_ident()))
        return None if browser_state == "unavailable" else Page()

    def get(session, url, **options):
        if url == START:
            timeline.append(("HTTP", get_ident()))
            return Response(url, status=403)
        assert url == "https://cdn.example/supp_1.pdf"
        assert session.headers["User-Agent"] == "offline-test-browser"
        timeline.append(("attachment", get_ident()))
        return Response(url)

    monkeypatch.setattr(supp.requests.Session, "get", get)
    monkeypatch.setattr(browser_engine, "get_browser_page", page_for)
    with ThreadPoolExecutor(1) as pool:

        def lane(host, callback):
            assert host == "publisher.example"
            return pool.submit(callback).result()

        result = supp.fetch_supplementary("10.1/paper", tmp_path, run_browser=lane)
    assert timeline[0] == ("HTTP", thread)
    assert len({owner for _, owner in timeline[1:]}) == 1
    assert timeline[1][1] != thread
    if browser_state == "success":
        assert [action for action, _ in timeline] == [
            "HTTP",
            "open",
            "go",
            "attachment",
            "close",
        ]
        assert result.saved[0].item.label == "Browser Supplement"
    else:
        assert [action for action, _ in timeline] == (
            ["HTTP", "open", "go", "close"]
            if browser_state == "navigation_error"
            else ["HTTP", "open"]
        )
        assert (result.pages_read, result.pages_unread) == (0, 1)
        assert not result.saved and not result.unsaved


@pytest.mark.parametrize("clears", [True, False])
def test_non_science_browser_challenge_wait_and_signed_cdn_route(
    tmp_path, monkeypatch, publisher, clears
):
    article = "https://academic.oup.com/fixture/article"
    cdn = "https://oup.silverchair-cdn.com/supp_1.pdf?signature=offline-fixture&part=1"
    publisher.article_url_template = article
    calls = []
    waits = []

    class Page:
        url = article
        context = SimpleNamespace(cookies=list)
        closed = False

        def goto(self, url, **kwargs):
            return SimpleNamespace(
                ok=False, url=url, headers={"cf-mitigated": "challenge"}
            )

        def wait_for_timeout(self, duration):
            waits.append(duration)

        def content(self):
            if len(waits) == 15:
                raise RuntimeError("page is navigating")
            if clears and len(waits) >= 25:
                return f'<a href="{cdn}">Supplement</a>'
            return "<title>Just a moment...</title>"

        def evaluate(self, _script):
            return "offline-test-browser"

        def evaluate_handle(self, *_args):
            pytest.fail("Cross-origin CDN attachments must not use in-page fetch")

        def close(self):
            self.closed = True

    page = Page()
    monkeypatch.setattr(browser_engine, "get_browser_page", lambda _: page)

    def get(self, url, **kwargs):
        calls.append(url)
        if url == article:
            return Response(url, status=403)
        assert url == cdn  # Preserve the entire query when making the request.
        return Response(url, chunks=(b"%PDF-1.7",))

    monkeypatch.setattr(supp.requests.Session, "get", get)
    result = supp.fetch_supplementary("10.1093/fixture", tmp_path)
    assert page.closed
    if clears:
        assert calls == [article, cdn]
        assert result.saved[0].path.read_bytes() == b"%PDF-1.7"
    else:
        assert sum(waits) == 30_000
        assert calls == [article]
        assert not result.saved and result.pages_unread == 1


class FakeTabStream:
    def __init__(
        self,
        url,
        body,
        *,
        mime="application/octet-stream",
        declared="",
        status=200,
        fail=False,
    ):
        self.meta = {
            "url": url,
            "type": mime,
            "length": declared,
            "status": status,
            "ok": status == 200,
        }
        self.body = body
        self.fail = fail
        self.reads = []
        self.cancelled = False
        self.disposed = False

    def evaluate(self, script, limit=None):
        if "s.response.ok" in script:
            return self.meta
        if "s.reader =" in script:
            return None
        if "clearTimeout" in script:
            self.cancelled = (
                "s.controller.abort()" in script and "s.reader?.cancel()" in script
            )
            return None
        assert "s.reader.read()" in script
        assert 0 < limit <= 1024 * 1024
        self.reads.append(limit)
        if self.fail:
            raise RuntimeError("private transport details")
        data, self.body = self.body[:limit], self.body[limit:]
        return {"done": not self.body, "data": b64encode(data).decode("ascii")}

    def dispose(self):
        self.disposed = True


def install_attachment_tab(monkeypatch, publisher, streams):
    class Page:
        url = ARTICLE
        context = SimpleNamespace(cookies=list)
        closed = False
        navigations = 0

        def goto(self, _url, **kwargs):
            self.navigations += 1
            return SimpleNamespace(ok=True, url=ARTICLE)

        def wait_for_timeout(self, _):
            pass

        def content(self):
            return "<p>Supplements</p>"

        def evaluate(self, _script):
            return "offline-test-browser"

        def evaluate_handle(self, script, options):
            assert not self.closed
            assert "credentials: 'include'" in script and "redirect:" not in script
            assert options["timeout"] == 30_000 and "controller.abort()" in script
            return streams[options["url"]]

        def close(self):
            self.closed = True

    page = Page()
    monkeypatch.setattr(browser_engine, "get_browser_page", lambda _: page)
    publisher.supplementary_items = lambda *_: [
        supp.SupplementaryItem(url) for url in streams
    ]
    return page


def test_same_origin_attachments_stream_multiple_types_from_article_tab(
    tmp_path, monkeypatch, publisher
):
    cases = {
        "sample.pdf": ("application/pdf", b"%PDF-1.7"),
        "data.zip": ("application/zip", b"PK sample"),
        "movie.mp4": ("video/mp4", b"offline movie"),
    }
    streams = {
        f"https://publisher.example/{name}": FakeTabStream(
            "https://publisher.example/redirected",
            data,
            mime=mime,
            declared=str(len(data)),
        )
        for name, (mime, data) in cases.items()
    }
    page = install_attachment_tab(monkeypatch, publisher, streams)

    def get(self, url, **options):
        assert (
            url == START
        )  # Only the failed plain-HTTP article request reaches this client.
        return Response(url, status=403)

    monkeypatch.setattr(supp.requests.Session, "get", get)
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    assert not result.unsaved
    assert {entry.item.filename: entry.path.read_bytes() for entry in result.saved} == {
        name: data for name, (_, data) in cases.items()
    }
    assert [entry.path.suffix for entry in result.saved] == [".pdf", ".zip", ".mp4"]
    assert all(
        stream.reads and stream.cancelled and stream.disposed
        for stream in streams.values()
    )
    assert page.navigations == 1 and page.closed


@pytest.mark.parametrize("declared", [True, False])
def test_browser_size_limit_cancels_without_external_retry(
    tmp_path, monkeypatch, publisher, declared
):
    url = "https://publisher.example/large.zip"
    stream = FakeTabStream(url, b"PK" + b"x" * 40, declared="42" if declared else "")
    page = install_attachment_tab(monkeypatch, publisher, {url: stream})

    def get(self, url, **kwargs):
        assert (
            url == START
        )  # A size rejection must not retry with a different transport.
        return Response(url, status=403)

    monkeypatch.setattr(supp.requests.Session, "get", get)
    monkeypatch.setattr(supp, "MAX_FILE_BYTES", 8)
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    (skipped,) = result.unsaved
    assert skipped.reason == (
        "too large (42 bytes; limit 8 bytes)"
        if declared
        else "too large (at least 9 bytes; limit 8 bytes)"
    )
    assert stream.reads == ([] if declared else [9])
    assert stream.cancelled and stream.disposed and page.closed
    assert not list(tmp_path.iterdir())


def test_browser_streams_share_paper_byte_budget(tmp_path, monkeypatch, publisher):
    first = "https://publisher.example/first.zip"
    second = "https://publisher.example/second.zip"
    streams = {
        first: FakeTabStream(first, b"PKabcd"),
        second: FakeTabStream(second, b"PKabc"),
    }
    page = install_attachment_tab(monkeypatch, publisher, streams)

    def get(self, url, **kwargs):
        assert url == START  # The browser quota must not fall back to external HTTP.
        return Response(url, status=403)

    monkeypatch.setattr(supp.requests.Session, "get", get)
    monkeypatch.setattr(supp, "MAX_PAPER_BYTES", 10)
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    assert [entry.path.read_bytes() for entry in result.saved] == [b"PKabcd"]
    (skipped,) = result.unsaved
    assert skipped.reason == "over the per-paper size limit (10 bytes)"
    assert streams[second].reads == [5] and streams[second].cancelled
    assert page.closed and not list(tmp_path.glob("*.part"))


def test_browser_failure_retries_external_http_once_and_reports_both_failures(
    tmp_path, monkeypatch, publisher
):
    root = "https://publisher.example/"
    streams = {
        root + "recover.zip": FakeTabStream(root + "recover.zip", b"PK", fail=True),
        root + "truncated.pdf": FakeTabStream(
            root + "truncated.pdf", b"%PDF-", declared="100"
        ),
        root + "stub.pdf": FakeTabStream(
            root + "stub.pdf", b"<html>login</html>", mime="application/pdf"
        ),
    }
    page = install_attachment_tab(monkeypatch, publisher, streams)
    calls = []

    def get(self, url, **kwargs):
        assert not page.closed
        calls.append(url)
        return Response(url, status=200 if url.endswith("recover.zip") else 403)

    monkeypatch.setattr(supp.requests.Session, "get", get)
    result = supp.fetch_supplementary("10.1/paper", tmp_path)
    assert calls == [START, *streams]
    (saved,) = result.saved
    assert saved.item.url.endswith("recover.zip")
    assert [entry.reason for entry in result.unsaved] == [
        "in-page fetch: truncated transfer (5 of 100 bytes); external HTTP: HTTP 403",
        "in-page fetch: not a file (HTML page); external HTTP: HTTP 403",
    ]
    assert all(stream.cancelled and stream.disposed for stream in streams.values())
    assert page.closed and not list(tmp_path.glob("*.part"))


def test_browser_tab_closes_when_item_discovery_raises(
    tmp_path, monkeypatch, publisher
):
    page = install_attachment_tab(monkeypatch, publisher, {})

    def fail(*args):
        raise ValueError("fixture hook failure")

    publisher.supplementary_items = fail
    monkeypatch.setattr(
        supp.requests.Session,
        "get",
        lambda *_args, **_kwargs: Response(START, status=403),
    )
    with pytest.raises(ValueError, match="fixture hook failure"):
        supp.fetch_supplementary("10.1/paper", tmp_path)
    assert page.closed


@pytest.mark.parametrize("key_present", [True, False])
def test_elsevier_api_credentials_stay_on_the_official_origin(
    tmp_path, monkeypatch, publisher, key_present
):
    api_page = "https://api.elsevier.com/content/article/doi/10.1016/paper"
    api_file = "https://api.elsevier.com/content/object/eid/item"
    cdn = "https://cdn.example/item.zip"
    publisher.supplementary_pages = lambda doi: [api_page, START]
    publisher.supplementary_items = lambda html, url: (
        [supp.SupplementaryItem(api_file)] if url == api_page else None
    )
    calls = []
    config = {"elsevier_insttoken": "fixture-inst"}
    monkeypatch.delenv("ELSEVIER_API_KEY", raising=False)
    if key_present:
        monkeypatch.setenv("ELSEVIER_API_KEY", "fixture-api-key")
    responses = {
        api_page: Response(api_page, text="<article/>"),
        api_file: Response(api_file, status=302, headers={"Location": cdn}),
        cdn: Response(cdn),
        START: Response(ARTICLE, text='<a href="supp_1.zip">Web supplement</a>'),
        "https://publisher.example/final/supp_1.zip": Response(
            "https://publisher.example/final/supp_1.zip"
        ),
    }

    def get(self, url, **options):
        calls.append(url)
        headers = options.get("headers", {})
        if url in {api_page, api_file}:
            assert headers["Accept"] == (
                "application/xml" if url == api_page else "*/*"
            )
            assert headers["X-ELS-APIKey"] == "fixture-api-key"
            assert headers["X-ELS-Insttoken"] == "fixture-inst"
            assert options["allow_redirects"] is False
        else:
            assert not headers
        return responses[url]

    monkeypatch.setattr(supp.requests.Session, "get", get)
    result = supp.fetch_supplementary(
        "10.1016/paper",
        tmp_path,
        config,
        run_browser=lambda *_: pytest.fail("No browser expected"),
    )
    assert len(result.saved) == 1
    assert result.saved[0].page_url == (api_page if key_present else ARTICLE)
    assert calls == (
        [api_page, api_file, cdn]
        if key_present
        else [START, "https://publisher.example/final/supp_1.zip"]
    )
    assert result.route == ("Elsevier API" if key_present else "publisher")


def test_placeholder_disposition_name_does_not_override_the_listed_filename():
    from scansci_pdf import supplementary

    item = supplementary.SupplementaryItem(
        "https://api.elsevier.com/content/object/eid/1-s2.0-S0-mmc1.xlsx",
        "Table S1",
        "",
        "mmc1.xlsx",
    )
    named, extension = supplementary._download_name(
        item, item.url, 'attachment; filename="f.txt"'
    )
    assert (named.filename, extension) == ("mmc1.xlsx", ".xlsx")
