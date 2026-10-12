"""Exercise admission and recovery through real local HTTP responses."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit, urlunsplit

import fitz
import pytest
import requests

from scansci_pdf import network, upstream_client
from scansci_pdf.pdf_utils import download_pdf
from scansci_pdf.sources.arxiv import download_arxiv_pdf
from scansci_pdf.sources.scihub import try_scihub_domain
from scansci_pdf.upstream_client import (
    get_json,
    get_text,
    openalex_content_url,
    openalex_params,
)
from scansci_pdf.upstream_errors import RateLimited, UpstreamError


@dataclass
class LocalUpstream:
    routes: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    arrived: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    url: str = ""


@pytest.fixture(autouse=True)
def isolated_admission(monkeypatch):
    # Each test starts a new process-equivalent controller, not a reset API.
    monkeypatch.setattr(upstream_client, "_admission", upstream_client._Admission())


@pytest.fixture
def upstream():
    upstream = LocalUpstream()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            parsed = urlsplit(self.path)
            with upstream.lock:
                upstream.calls.append(
                    (
                        time.monotonic(),
                        parsed.path,
                        parse_qs(parsed.query),
                        dict(self.headers),
                    )
                )
                route = upstream.routes.get(parsed.path, (200, {}, b'{"ok": true}'))
            upstream.arrived.set()
            if route is None:
                self.close_connection = True
                self.connection.close()
                return
            status, headers, body = route
            self.send_response(status)
            self.send_header(
                "Content-Length", headers.get("Content-Length", str(len(body)))
            )
            for name, value in headers.items():
                if name != "Content-Length":
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    upstream.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield upstream
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.fixture
def session(upstream, monkeypatch):
    class LocalSession(requests.Session):
        def request(self, method, url, **kwargs):
            # Only the transport is redirected; admission sees the actual host.
            parsed = urlsplit(url)
            local = urlsplit(upstream.url)
            url = urlunsplit(
                (local.scheme, local.netloc, parsed.path, parsed.query, "")
            )
            return super().request(method, url, **kwargs)

    with LocalSession() as session:
        session.trust_env = False
        monkeypatch.setattr(network, "_get_session", lambda config: session)
        yield session


def test_fetch_and_json_share_crossref_start_spacing(upstream, session):
    url = "https://api.crossref.org/works/first"
    with network.fetch(url, {}) as response:
        assert response.json() == {"ok": True}
    assert get_json("https://api.crossref.org/works/second", {}, session=session) == {
        "ok": True
    }
    assert get_json("https://api.crossref.org/works/third", {}, session=session) == {
        "ok": True
    }
    starts = [call[0] for call in upstream.calls]
    assert all(later - earlier >= 0.18 for earlier, later in pairwise(starts))


def test_openalex_uses_ten_rps_not_two(upstream, session):
    for number in range(3):
        assert get_json(
            f"https://api.openalex.org/works/{number}",
            {},
            session=session,
            operation="lookup",
        ) == {"ok": True}
    starts = [call[0] for call in upstream.calls]
    assert all(later - earlier >= 0.09 for earlier, later in pairwise(starts))
    assert starts[-1] - starts[0] < 0.8


def test_export_arxiv_start_spacing(upstream, session):
    assert get_text("https://export.arxiv.org/api/query", {}, session=session)
    assert get_text("https://export.arxiv.org/api/query", {}, session=session)
    assert upstream.calls[1][0] - upstream.calls[0][0] >= 2.9


def test_stream_holds_shared_slot_and_does_not_block_another_service(upstream, session):
    first = network.fetch("https://api.crossref.org/works/first", {}, stream=True)
    requested = threading.Event()

    def second():
        requested.set()
        return get_json("https://api.crossref.org/works/second", {}, session=session)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(second)
            assert requested.wait(timeout=1)
            try:
                assert get_json(
                    "https://api.openalex.org/works/free", {}, session=session
                ) == {"ok": True}
                # This exceeds Crossref's pacing interval, so only its occupied
                # stream slot can prevent the second request from starting.
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.3)
                assert [call[1] for call in upstream.calls] == [
                    "/works/first",
                    "/works/free",
                ]
            finally:
                first.close()
            assert future.result(timeout=1) == {"ok": True}
    finally:
        first.close()
    assert [call[1] for call in upstream.calls][-1] == "/works/second"


def test_success_budget_blocks_search_and_content_but_allows_lookup_and_other_identity(
    upstream, session
):
    upstream.routes["/works/seed"] = (
        200,
        {
            "X-RateLimit-Remaining": "5",
            "X-RateLimit-Reset": "600",
        },
        b'{"id": "W1"}',
    )
    config = {"openalex_api_key": "account-one"}
    params = openalex_params(config)
    assert get_json(
        "https://api.openalex.org/works/seed", config, params=params, operation="lookup"
    ) == {"id": "W1"}
    for operation, url in [
        ("search", "https://api.openalex.org/works"),
        ("content", openalex_content_url("https://openalex.org/W1", config)),
    ]:
        with pytest.raises(RateLimited) as raised:
            get_json(url, config, params=params, operation=operation)
        error = raised.value
        assert error.kind == "daily_budget"
        assert error.remaining == 5
        assert error.required == (10 if operation == "search" else 100)
        assert time.time() + 590 < error.retry_at < time.time() + 610
        assert error.to_dict()["retry_at"].endswith("Z")
    assert get_json(
        "https://api.openalex.org/works/free", config, params=params, operation="lookup"
    ) == {"ok": True}
    assert get_json(
        "https://api.openalex.org/works",
        {},
        params={"api_key": "account-two"},
        operation="search",
    ) == {"ok": True}
    assert [call[1] for call in upstream.calls] == [
        "/works/seed",
        "/works/free",
        "/works",
    ]


def test_daily_429_reuses_reset_without_another_request(upstream, session):
    upstream.routes["/works"] = (
        429,
        {
            "X-RateLimit-Remaining": "5",
            "X-RateLimit-Reset": "60",
        },
        b'{"message": "secret key and full URL must never be copied"}',
    )
    params = {"api_key": "private-upstream-key", "search": "private-search-title"}
    before = time.time()
    with pytest.raises(RateLimited) as first:
        get_json(
            "https://api.openalex.org/works", {}, params=params, operation="search"
        )
    with pytest.raises(RateLimited) as second:
        get_json(
            "https://api.openalex.org/works", {}, params=params, operation="search"
        )
    assert len(upstream.calls) == 1
    assert first.value.kind == second.value.kind == "daily_budget"
    assert first.value.retry_at == second.value.retry_at
    assert before + 59 <= first.value.retry_at <= time.time() + 61
    serialized = json.dumps(second.value.to_dict()) + str(second.value)
    assert "OpenAlex" in str(second.value)
    assert "private-upstream-key" not in serialized
    assert "private-search-title" not in serialized
    assert "secret key" not in serialized
    assert "api.openalex.org" not in serialized


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("request_rate", b'{"message": "Exceeded 100 requests per second"}'),
        ("unknown", b'{"message": "Unclassified upstream restriction"}'),
    ],
)
def test_short_service_cooldown_is_shared_across_upstream_keys(
    upstream, session, kind, body
):
    upstream.routes["/works"] = (429, {"Retry-After": "2"}, body)
    with pytest.raises(RateLimited) as first:
        get_json(
            "https://api.openalex.org/works",
            {},
            params={"api_key": "account-one"},
            operation="search",
        )
    with pytest.raises(RateLimited) as second:
        get_json(
            "https://api.openalex.org/works/free",
            {},
            params={"api_key": "account-two"},
            operation="lookup",
        )
    assert first.value.kind == second.value.kind == kind
    assert first.value.retry_at == second.value.retry_at
    assert len(upstream.calls) == 1
    assert get_json("https://api.crossref.org/works/free", {}) == {"ok": True}
    assert [call[1] for call in upstream.calls] == ["/works", "/works/free"]


@pytest.mark.parametrize("retry_header", ["5", "http-date"])
def test_explicit_rps_429_uses_retry_after_and_free_lookup_cools_down(
    upstream, session, retry_header
):
    now = time.time()
    retry = (
        formatdate(now + 5, usegmt=True)
        if retry_header == "http-date"
        else retry_header
    )
    upstream.routes["/works"] = (
        429,
        {"Retry-After": retry},
        b'{"message": "Exceeded 100 requests per second"}',
    )
    with pytest.raises(RateLimited) as first:
        get_json("https://api.openalex.org/works", {}, operation="search")
    with pytest.raises(RateLimited) as second:
        get_json("https://api.openalex.org/works/free", {}, operation="lookup")
    assert first.value.kind == second.value.kind == "request_rate"
    assert now + 4 <= first.value.retry_at <= time.time() + 5.1
    assert first.value.retry_at == second.value.retry_at
    assert len(upstream.calls) == 1
    assert get_json("https://api.crossref.org/works/free", {}) == {"ok": True}


def test_retry_after_alone_is_unknown(upstream, session):
    upstream.routes["/works"] = (
        429,
        {"Retry-After": "1"},
        b'{"message": "Rate limit reached"}',
    )
    with pytest.raises(RateLimited) as first:
        get_json("https://api.openalex.org/works", {}, operation="search")
    with pytest.raises(RateLimited) as second:
        get_json("https://api.openalex.org/works/free", {}, operation="lookup")
    assert first.value.kind == second.value.kind == "unknown"
    assert len(upstream.calls) == 1


def test_actual_key_and_headers_are_sent_to_json_and_content(upstream, session):
    config = {"openalex_api_key": "configured-upstream-key"}
    params = openalex_params(config, {"search": "a title", "per-page": 100})
    assert get_json(
        "https://api.openalex.org/works",
        config,
        params=params,
        headers={"X-Caller": "retained"},
    ) == {"ok": True}
    content_url = openalex_content_url("https://openalex.org/W123", config)
    with network.fetch(content_url, config) as response:
        assert response.json() == {"ok": True}
    assert upstream.calls[0][2] == {
        "api_key": ["configured-upstream-key"],
        "search": ["a title"],
        "per-page": ["100"],
    }
    assert upstream.calls[0][3]["X-Caller"] == "retained"
    assert upstream.calls[1][1] == "/works/W123.pdf"
    assert upstream.calls[1][2]["api_key"] == ["configured-upstream-key"]


def test_http_json_text_and_network_errors_leave_next_call_usable(upstream, session):
    upstream.routes["/missing"] = (404, {}, b'{"private": "not surfaced"}')
    upstream.routes["/bad-json"] = (200, {}, b"not JSON")
    upstream.routes["/xml"] = (200, {"Content-Type": "application/xml"}, b"<feed />")
    upstream.routes["/disconnect"] = None
    with pytest.raises(UpstreamError) as missing:
        get_json("https://api.crossref.org/missing", {})
    assert missing.value.status_code == 404
    with pytest.raises(UpstreamError, match="invalid JSON"):
        get_json("https://api.crossref.org/bad-json", {})
    assert get_text("https://api.crossref.org/xml", {}) == "<feed />"
    with pytest.raises(UpstreamError, match="network error"):
        get_json("https://api.crossref.org/disconnect", {})
    with pytest.raises(requests.RequestException):
        network.fetch("https://api.crossref.org/disconnect", {})
    assert get_json("https://api.crossref.org/usable", {}) == {"ok": True}


def test_unexpected_runtime_error_propagates_and_releases_slot(session):
    class BrokenSession(requests.Session):
        def request(self, *args, **kwargs):
            raise RuntimeError("unexpected internal bug")

    with (
        BrokenSession() as broken,
        pytest.raises(RuntimeError, match="unexpected internal bug"),
    ):
        get_json("https://api.crossref.org/works/bug", {}, session=broken)
    assert _in_thread(
        lambda: get_json("https://api.crossref.org/works/usable", {}, session=session)
    ).result(timeout=1) == {"ok": True}


@pytest.mark.parametrize("consumer", ["generic", "arxiv"])
@pytest.mark.parametrize("failure", ["invalid_pdf", "http_error", "read_error"])
def test_stream_download_failure_allows_following_request(
    upstream, session, tmp_path, consumer, failure
):
    route = {
        "invalid_pdf": (200, {"Content-Type": "text/html"}, b"not a PDF"),
        "http_error": (404, {}, b"missing"),
        "read_error": (
            200,
            {
                "Content-Type": "application/pdf",
                "Content-Length": "1000",
                "Connection": "close",
            },
            b"%PDF-1.7",
        ),
    }[failure]
    upstream.routes["/bad.pdf"] = route
    url = "https://api.crossref.org/bad.pdf"
    if consumer == "generic":
        result = download_pdf(url, tmp_path / "out.pdf", {}, "test")
    else:
        result = download_arxiv_pdf(url, tmp_path / "out.pdf", {})
    assert result is None
    # A leaked stream would hold Crossref's only slot indefinitely.
    assert _in_thread(
        lambda: get_json("https://api.crossref.org/works/next", {})
    ).result(timeout=1) == {"ok": True}


def _in_thread(function):
    future = Future()

    def run():
        try:
            future.set_result(function())
        except Exception as exc:  # noqa: BLE001 - Forward worker failures to the assertion.
            future.set_exception(exc)

    # A regression should fail the bounded assertion, not hang the test process.
    threading.Thread(target=run, daemon=True).start()
    return future


def test_cooldown_and_daily_reset_expire_without_retrying(
    upstream, session, monkeypatch
):
    now = time.time()
    clock = SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
    monkeypatch.setattr(upstream_client, "time", clock)
    upstream.routes["/works"] = (
        429,
        {"Retry-After": "2"},
        b'{"error": "Unclassified upstream restriction"}',
    )
    with pytest.raises(RateLimited) as limited:
        get_json("https://api.openalex.org/works", {}, operation="search")
    assert limited.value.kind == "unknown"
    assert limited.value.retry_at == now + 2
    with pytest.raises(RateLimited):
        get_json("https://api.openalex.org/works", {}, operation="search")
    assert len(upstream.calls) == 1
    now += 3
    upstream.routes["/works"] = (
        200,
        {"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "60"},
        b'{"ok": true}',
    )
    assert get_json("https://api.openalex.org/works", {}, operation="lookup") == {
        "ok": True
    }
    with pytest.raises(RateLimited, match="daily budget"):
        get_json("https://api.openalex.org/works", {}, operation="search")
    assert len(upstream.calls) == 2
    now += 61
    upstream.routes["/works"] = (200, {}, b'{"ok": true}')
    assert get_json("https://api.openalex.org/works", {}, operation="search") == {
        "ok": True
    }
    assert len(upstream.calls) == 3


@pytest.mark.parametrize("consumer", ["generic", "arxiv", "scihub"])
def test_successful_stream_download_leaves_slot_usable(
    upstream, session, tmp_path, consumer
):
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text(
            (30, 30), "A real PDF used to exercise streamed downloads. " * 30
        )
        document.set_metadata({"subject": "Valid streamed PDF test metadata " * 100})
        body = document.tobytes()
    path = "/10.1/x" if consumer == "scihub" else "/good.pdf"
    upstream.routes[path] = (200, {"Content-Type": "application/pdf"}, body)
    output = tmp_path / "out.pdf"
    if consumer == "generic":
        result = download_pdf("https://api.crossref.org/good.pdf", output, {}, "test")
    elif consumer == "arxiv":
        result = download_arxiv_pdf("https://api.crossref.org/good.pdf", output, {})
    else:
        result = try_scihub_domain(
            "10.1/x",
            "https://api.crossref.org",
            output,
            {"scihub_browser_first": False},
        )
    assert result is not None
    with fitz.open(output) as downloaded:
        assert downloaded.page_count == 1
    assert _in_thread(
        lambda: get_json("https://api.crossref.org/works/next", {})
    ).result(timeout=1) == {"ok": True}


def test_scihub_closes_landing_before_same_host_pdf(upstream, session, tmp_path):
    body = f'<meta name="citation_pdf_url" content="{upstream.url}/full.pdf">'.encode()
    upstream.routes["/10.1/x"] = (200, {"Content-Type": "text/html"}, body)
    upstream.routes["/full.pdf"] = (404, {}, b"missing")
    config = {"scihub_browser_first": False, "host_concurrency": {"127.0.0.1": 1}}
    result = _in_thread(
        lambda: try_scihub_domain("10.1/x", upstream.url, tmp_path / "out.pdf", config)
    ).result(timeout=2)
    assert result is None
    assert [call[1] for call in upstream.calls] == ["/10.1/x", "/full.pdf"]
    assert _in_thread(lambda: get_json(upstream.url + "/works/next", config)).result(
        timeout=1
    ) == {"ok": True}


def test_json_probe_caches_success_and_propagates_429(upstream, session):
    path = f"/works/cache-{urlsplit(upstream.url).port}"
    url = "https://api.openalex.org" + path
    assert network.fetch_json(url, {"json_probe_cache_seconds": 60}) == {"ok": True}
    upstream.routes[path] = (
        429,
        {"Retry-After": "2"},
        b'{"message": "Upstream restriction"}',
    )
    assert network.fetch_json(url, {"json_probe_cache_seconds": 60}) == {"ok": True}
    assert len(upstream.calls) == 1
    with pytest.raises(RateLimited) as error:
        network.fetch_json(url, {"json_probe_cache_seconds": 0})
    assert error.value.kind == "unknown"
    assert len(upstream.calls) == 2
