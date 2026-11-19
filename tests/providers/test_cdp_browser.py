"""CDP lifecycle and page-origin byte transport; no real browser is launched."""

import base64
import logging
import sys
from types import ModuleType, SimpleNamespace

import pymupdf
import pytest

from scansci_pdf import browser_backend, browser_engine


class FakePage:
    def __init__(self, url="https://www.science.org/doi/10.1126/adh2586"):
        self.url = url
        self.closed = False
        self.requests = []
        self.payload = b""
        self.status = 200
        self.content_type = "application/pdf"
        self.result_url = "https://www.science.org/doi/pdf/10.1126/adh2586"
        self.broken = False
        self.timeout_after_chunk = False
        self.headers_timeout = False
        self.aborted = False
        self.cancelled = False
        self.on_close = lambda page: None

    def on(self, event, callback):
        assert event == "close"
        self.on_close = callback

    def close(self):
        self.closed = True
        self.on_close(self)

    def evaluate_handle(self, script, argument):
        assert all(
            operation in script
            for operation in (
                "AbortController",
                "setTimeout",
                "controller.abort()",
                "signal: controller.signal",
            )
        )
        assert argument["timeout"] > 0
        assert "credentials: 'same-origin'" in script
        assert "headers:" not in script and "application/pdf" not in script
        self.requests.append(argument["url"])
        if self.headers_timeout:
            raise TimeoutError("secret-url-query=never-log")
        return FakeSession(self)


class FakeSession:
    def __init__(self, page):
        self.page = page
        self.position = 0
        self.disposed = False

    def evaluate(self, script, limit=None):
        if "s.response.ok" in script:
            return {
                "ok": self.page.status == 200,
                "status": self.page.status,
                "type": self.page.content_type,
                "url": self.page.result_url,
            }
        if "s.reader =" in script:
            return None
        if "clearTimeout" in script:
            self.page.aborted = "s.controller.abort()" in script
            self.page.cancelled = "s.reader?.cancel()" in script
            return None
        assert "s.reader.read()" in script and "Date.now() >= s.deadline" in script
        if self.page.timeout_after_chunk and self.position:
            raise TimeoutError("browser AbortController reached deadline")
        if self.page.broken and self.position:
            raise OSError("stream interrupted")
        assert 0 < limit <= 1024 * 1024
        data = self.page.payload[self.position : self.position + min(32768, limit)]
        self.position += len(data)
        exhausted = self.position == len(self.page.payload)
        return {
            "done": exhausted
            and not (self.page.broken or self.page.timeout_after_chunk),
            "data": base64.b64encode(data).decode("ascii"),
        }

    def dispose(self):
        self.disposed = True


def test_borrowed_context_and_reconnect(monkeypatch):
    config = {"browser_backend": "cdp", "browser_cdp_url": "http://127.0.0.1:9222"}
    records = []

    def start():
        existing = FakePage()
        context = SimpleNamespace(pages=[existing])
        context.new_page = lambda: context.pages.append(FakePage()) or context.pages[-1]
        context.close = lambda: pytest.fail("borrowed context must never close")
        browser = SimpleNamespace(contexts=[context], connected=True)
        browser.is_connected = lambda: browser.connected
        browser.close = lambda: records.append("disconnect")
        browser.new_context = lambda: pytest.fail("must reuse default context")
        driver = SimpleNamespace(stop=lambda: records.append("stop"))
        records.append((browser, context, existing))
        return browser_backend.BorrowedCDPSession(browser, context, driver)

    monkeypatch.setattr(browser_engine, "connect_cdp", lambda cfg: start())
    monkeypatch.setattr(browser_engine, "_check_browser_backend", lambda cfg: True)
    try:
        _, context = browser_engine._get_shared_browser(config)
        own = browser_engine._new_page(context)
        browser_engine.close_shared_browser()
        assert own.closed
        assert not records[0][2].closed
        assert records[1:3] == ["disconnect", "stop"]
        browser, _ = browser_engine._get_shared_browser(config)
        browser.connected = False
        browser_engine._get_shared_browser(config)
        assert len([entry for entry in records if isinstance(entry, tuple)]) == 3
    finally:
        browser_engine.close_shared_browser()


def test_cdp_missing_url_and_default_context_never_launch(monkeypatch):
    with pytest.raises(RuntimeError, match="browser_cdp_url"):
        browser_backend.connect_cdp({"browser_backend": "cdp"})
    monkeypatch.setattr(browser_backend, "is_available", lambda name: True)
    assert browser_backend.resolve_backend({"browser_backend": "cdp"}) == "cdp"
    with pytest.raises(RuntimeError, match="use connect_cdp"):
        browser_backend.launch(config={"browser_backend": "cdp"})

    browser = SimpleNamespace(contexts=[], close=lambda: None)
    driver = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=lambda url, **kw: browser),
        stop=lambda: None,
    )
    playwright = ModuleType("playwright")
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: SimpleNamespace(start=lambda: driver)
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    with pytest.raises(RuntimeError, match="no existing default context"):
        browser_backend.connect_cdp({"browser_cdp_url": "http://localhost:9222"})


def test_cdp_probe_disconnects_without_touching_default_context(monkeypatch):
    calls = []
    context = SimpleNamespace(
        new_page=lambda: pytest.fail("probe must not create a page"),
        close=lambda: pytest.fail("probe must not close default context"),
    )
    browser = SimpleNamespace(
        contexts=[context], close=lambda: calls.append("disconnect")
    )
    driver = SimpleNamespace(
        chromium=SimpleNamespace(
            connect_over_cdp=lambda url, **kwargs: (
                calls.append((url, kwargs)) or browser
            )
        ),
        stop=lambda: calls.append("stop"),
    )
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: SimpleNamespace(start=lambda: driver)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    for url in ("http://localhost:9222", "ws://localhost:9222/devtools/browser/id"):
        browser_backend.probe_cdp({"browser_cdp_url": url, "browser_cdp_timeout": 0.2})
        assert calls[-3:] == [
            (url, {"no_defaults": True, "timeout": 200.0}),
            "disconnect",
            "stop",
        ]
    for url in ("ftp://localhost:9222", "http://", "http://localhost:bad"):
        with pytest.raises(RuntimeError, match="valid HTTP\\(S\\) or WS\\(S\\) URL"):
            browser_backend.connect_cdp({"browser_cdp_url": url})


def test_cdp_connection_failure_is_not_local_fallback(monkeypatch):
    config = {"browser_backend": "cdp", "browser_cdp_url": "http://127.0.0.1:9222"}
    monkeypatch.setattr(browser_engine, "_check_browser_backend", lambda cfg: True)
    monkeypatch.setattr(
        browser_engine,
        "connect_cdp",
        lambda cfg: (_ for _ in ()).throw(ConnectionRefusedError("offline")),
    )
    monkeypatch.setattr(
        browser_engine, "launch", lambda **kw: pytest.fail("local launch is forbidden")
    )
    browser_engine.close_shared_browser()
    with pytest.raises(RuntimeError, match="CDP browser tab unavailable: offline"):
        browser_engine.create_tab("about:blank", config)


def test_science_existing_dom_path_uses_borrowed_tab(monkeypatch, tmp_path):
    from scansci_pdf import _publisher_strategies_core as strategy
    from scansci_pdf.publisher_strategies import science

    monkeypatch.setattr(science, "_wait_for_science_reader", lambda *a, **kw: None)
    html = '<html><nav>Get access | Institutional access | Subscribe</nav><a href="/doi/pdf/10.1126/adh2586">View PDF</a></html>'
    assert strategy._detect_paywall(html)
    actions = []
    monkeypatch.setattr(browser_engine, "is_available", lambda config: True)
    monkeypatch.setattr(browser_engine, "create_tab", lambda *a, **kw: "own-tab")
    monkeypatch.setattr(browser_engine, "navigate_tab", lambda *a, **kw: True)
    monkeypatch.setattr(
        browser_engine, "close_tab", lambda *a: actions.append("close-tab")
    )
    monkeypatch.setattr(
        browser_engine,
        "evaluate_js",
        lambda tab, js, config, **kw: (
            html if "outerHTML" in js else "https://www.science.org/doi/10.1126/adh2586"
        ),
    )
    monkeypatch.setattr(strategy.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        strategy, "_inject_cookies_to_tab", lambda *a: actions.append("inject")
    )

    def fetch(tab, url, path, config):
        actions.append((tab, url))
        document = pymupdf.open()
        for _ in range(7):
            document.new_page()
        document.save(path)
        document.close()
        return True

    monkeypatch.setattr(browser_engine, "fetch_pdf_in_tab", fetch)
    output = tmp_path / "science.pdf"
    result = strategy._browser_download(
        "10.1126/adh2586",
        "https://www.science.org/doi/10.1126/adh2586",
        output,
        {"browser_backend": "cdp", "interactive": False},
        "Science",
    )
    assert result
    assert ("own-tab", "https://www.science.org/doi/pdf/10.1126/adh2586") in actions
    assert actions[-1] == "close-tab"

    # A later invalid candidate must not replace the first transport timeout.
    failures = []

    def failed_fetch(tab, url, path, config):
        failures.append(url)
        browser_engine._tls.pdf_fetch_reason = (
            "network_error" if len(failures) == 1 else "no_pdf_found"
        )
        browser_engine._tls.pdf_fetch_error = (
            "headers: AbortError; bytes=0; elapsed=120.0s"
            if len(failures) == 1
            else "headers: response rejected; bytes=0; elapsed=0.2s; status=403; mime=text/html"
        )
        return False

    monkeypatch.setattr(browser_engine, "fetch_pdf_in_tab", failed_fetch)
    assert not strategy._browser_download(
        "10.1126/adh2586",
        "https://www.science.org/doi/10.1126/adh2586",
        tmp_path / "failed.pdf",
        {"browser_backend": "cdp", "interactive": False},
        "Science",
    )
    assert strategy.get_last_error() == (
        "network_error",
        "headers: AbortError; bytes=0; elapsed=120.0s",
    )
    assert len(failures) > 1


@pytest.mark.parametrize(
    "backend,scenario",
    [
        ("cdp", "success"),
        ("cdp", "challenge"),
        ("cdp", "disconnected"),
        ("patchright", "navigate_failed"),
    ],
)
def test_public_science_handler_uses_verified_pdf_entry(
    monkeypatch, tmp_path, backend, scenario
):
    from scansci_pdf import _publisher_strategies_core as strategy
    from scansci_pdf.sources.publishers import get_publisher_fast_sources

    doi = "10.1126/adh2586"
    handlers = {label: fn for fn, label in get_publisher_fast_sources(doi)}
    assert "ScienceBrowser" in handlers
    navigated = []
    fetched = []
    monkeypatch.setattr(strategy, "_has_publisher_cookies", lambda config: False)
    monkeypatch.setattr(strategy, "_inject_cookies_to_tab", lambda *args: None)
    monkeypatch.setattr(strategy.time, "sleep", lambda seconds: None)
    from scansci_pdf.publisher_strategies import science

    monkeypatch.setattr(science, "_wait_for_science_reader", lambda *a, **kw: None)
    monkeypatch.setattr(science, "_science_fetch_download", lambda *a, **kw: False)
    monkeypatch.setattr(browser_engine, "is_available", lambda config: True)
    monkeypatch.setattr(browser_engine, "close_tab", lambda *args: None)

    def create(url, config, **kwargs):
        if scenario == "disconnected":
            raise RuntimeError("CDP connection refused")
        assert url == "about:blank"
        return "our-tab"

    def navigate(tab, url, config, **kwargs):
        navigated.append(url)
        return scenario != "navigate_failed"

    def evaluate(tab, js, config, **kwargs):
        if "outerHTML" in js:
            return (
                '<title>Just a moment...</title><div id="challenge-platform">Checking</div>'
                if scenario == "challenge"
                else "<html><body>PDF viewer</body></html>"
            )
        return navigated[-1]

    def fetch(tab, url, path, config):
        fetched.append(url)
        document = pymupdf.open()
        for _ in range(7):
            document.new_page()
        document.save(path)
        document.close()
        return True

    monkeypatch.setattr(browser_engine, "create_tab", create)
    monkeypatch.setattr(browser_engine, "navigate_tab", navigate)
    monkeypatch.setattr(browser_engine, "evaluate_js", evaluate)
    monkeypatch.setattr(browser_engine, "fetch_pdf_in_tab", fetch)
    output = tmp_path / "paper.pdf"
    config = {"browser_backend": backend, "interactive": False}
    if scenario == "disconnected":
        with pytest.raises(RuntimeError, match="CDP connection refused"):
            handlers["ScienceBrowser"](doi, output, config)
    else:
        result = handlers["ScienceBrowser"](doi, output, config)
        if scenario == "success":
            assert result and result["success"]
            assert output.exists()
            assert fetched == [f"/doi/pdf/{doi}"]
        else:
            assert not result and not output.exists() and not fetched
            if scenario == "challenge":
                assert strategy.get_last_error()[0] == "cloudflare_blocked"
    expected = (
        f"https://www.science.org/doi/epdf/{doi}"
        if backend == "cdp"
        else f"https://doi.org/{doi}"
    )
    if scenario != "disconnected":
        assert navigated == [expected]


def test_challenge_stays_distinct_from_paywall(monkeypatch, tmp_path):
    from scansci_pdf import _publisher_strategies_core as strategy
    from scansci_pdf.publisher_strategies import science

    monkeypatch.setattr(science, "_wait_for_science_reader", lambda *a, **kw: None)
    challenge = '<html><title>Just a moment...</title><div id="challenge-platform">Checking your browser</div></html>'
    calls = []
    monkeypatch.setattr(browser_engine, "is_available", lambda config: True)
    monkeypatch.setattr(browser_engine, "create_tab", lambda *a, **kw: "own-tab")
    monkeypatch.setattr(browser_engine, "navigate_tab", lambda *a, **kw: True)
    monkeypatch.setattr(browser_engine, "close_tab", lambda *a: calls.append("closed"))
    monkeypatch.setattr(
        browser_engine,
        "evaluate_js",
        lambda tab, js, config, **kw: (
            challenge
            if "outerHTML" in js
            else "https://www.science.org/doi/10.1126/adh2586"
        ),
    )
    monkeypatch.setattr(strategy.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(strategy, "_inject_cookies_to_tab", lambda *a: None)
    monkeypatch.setattr(
        browser_engine, "fetch_pdf_in_tab", lambda *a: calls.append("fetch")
    )
    assert not strategy._browser_download(
        "10.1126/adh2586",
        "https://www.science.org/doi/10.1126/adh2586",
        tmp_path / "blocked.pdf",
        {"browser_backend": "cdp", "interactive": False},
        "Science",
    )
    assert strategy.get_last_error()[0] == "cloudflare_blocked"
    assert calls == ["closed"]


def test_stream_pdf_and_reject_errors(monkeypatch, tmp_path):
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Sample browser PDF")
    payload = document.tobytes()
    document.close()
    page = FakePage()
    page.payload = payload
    monkeypatch.setattr(browser_engine, "_resolve_tab", lambda tab: page)
    output = tmp_path / "article.pdf"
    config = {"browser_backend": "cdp"}
    assert browser_engine.fetch_pdf_in_tab(
        "tab", "/doi/pdf/10.1126/adh2586", output, config
    )
    assert output.read_bytes() == payload
    assert page.requests == [page.result_url]
    assert not (tmp_path / "article.pdf.part").exists()

    output.unlink()
    page.requests.clear()
    page.headers_timeout = True
    assert not browser_engine.fetch_pdf_in_tab(
        "tab", "/doi/pdf/10.1126/adh2586", output, config
    )
    header_error = browser_engine.last_pdf_fetch_error()
    assert "headers: TimeoutError; bytes=0; elapsed=" in header_error
    assert "status=" not in header_error and "mime=" not in header_error
    assert "secret-url-query" not in header_error
    assert browser_engine.last_pdf_fetch_reason() == "network_error"
    assert not output.exists()
    page.headers_timeout = False
    page.requests.clear()
    assert not browser_engine.fetch_pdf_in_tab(
        "tab", "https://other.example/pdf", output, config
    )
    assert page.requests == []
    page.status = 403
    assert not browser_engine.fetch_pdf_in_tab(
        "tab", "/doi/pdf/10.1126/adh2586", output, config
    )
    assert not output.exists()
    assert (
        "headers: response rejected; bytes=0; elapsed="
        in browser_engine.last_pdf_fetch_error()
    )
    assert "status=403; mime=application/pdf" in browser_engine.last_pdf_fetch_error()
    assert browser_engine.last_pdf_fetch_reason() == "no_pdf_found"
    page.status = 200
    page.content_type = "text/html"
    assert not browser_engine.fetch_pdf_in_tab(
        "tab", "/doi/pdf/10.1126/adh2586", output, config
    )
    assert "status=200; mime=text/html" in browser_engine.last_pdf_fetch_error()
    page.content_type = "application/pdf"
    page.result_url = "https://other.example/pdf"
    assert not browser_engine.fetch_pdf_in_tab(
        "tab", "/doi/pdf/10.1126/adh2586", output, config
    )
    page.result_url = "https://www.science.org/doi/pdf/10.1126/adh2586"
    page.broken = True
    assert not browser_engine.fetch_pdf_in_tab(
        "tab", "/doi/pdf/10.1126/adh2586", output, config
    )
    assert not output.exists()
    assert not (tmp_path / "article.pdf.part").exists()
    assert page.aborted and page.cancelled
    assert "body: OSError; bytes=" in browser_engine.last_pdf_fetch_error()
    assert "status=200; mime=application/pdf" in browser_engine.last_pdf_fetch_error()

    page.broken = False
    page.aborted = page.cancelled = False
    page.timeout_after_chunk = True
    assert not browser_engine.fetch_pdf_in_tab(
        "tab",
        "/doi/pdf/10.1126/adh2586",
        output,
        {"browser_pdf_timeout": 0.01},
    )
    assert page.aborted and page.cancelled
    assert "body: TimeoutError; bytes=" in browser_engine.last_pdf_fetch_error()
    assert browser_engine.last_pdf_fetch_reason() == "network_error"
    assert "elapsed=" in browser_engine.last_pdf_fetch_error()
    assert not output.exists() and not (tmp_path / "article.pdf.part").exists()

    page.timeout_after_chunk = False
    page.aborted = page.cancelled = False
    assert not browser_engine.fetch_pdf_in_tab(
        "tab",
        "/doi/pdf/10.1126/adh2586",
        output,
        {"browser_pdf_max_bytes": 10},
    )
    assert page.aborted and page.cancelled
    assert (
        "body: PDF exceeds configured byte limit; bytes=0"
        in browser_engine.last_pdf_fetch_error()
    )
    assert not output.exists() and not (tmp_path / "article.pdf.part").exists()

    page.payload = b"not a PDF"
    assert not browser_engine.fetch_pdf_in_tab(
        "tab", "/doi/pdf/10.1126/adh2586", output, config
    )
    assert (
        "validation: invalid PDF header; bytes=9; elapsed="
        in browser_engine.last_pdf_fetch_error()
    )
    assert "status=200; mime=application/pdf" in browser_engine.last_pdf_fetch_error()


@pytest.mark.parametrize("scenario", ["complete", "truncated", "oversized"])
def test_pdf_base64_batches_validate_and_clean_up(monkeypatch, tmp_path, scenario):
    with pymupdf.open() as document:
        document.new_page()
        payload = document.tobytes()

    class BatchSession(FakeSession):
        def evaluate(self, script, limit=None):
            if limit is None:
                return super().evaluate(script)
            assert "toBase64" in script and "btoa" in script
            limits.append(limit)
            if len(limits) == 1:
                return {"done": False, "data": base64.b64encode(payload[:5]).decode()}
            encoded = base64.b64encode(payload[5:]).decode()
            if scenario == "truncated":
                encoded = encoded[:-1]
            return {"done": True, "data": encoded}

    limits = []
    page = FakePage()
    session = BatchSession(page)
    monkeypatch.setattr(page, "evaluate_handle", lambda *args: session)
    monkeypatch.setattr(browser_engine, "_resolve_tab", lambda tab: page)
    output = tmp_path / "batches.pdf"
    max_bytes = 8 if scenario == "oversized" else len(payload)
    result = browser_engine.fetch_pdf_in_tab(
        "tab", page.result_url, output, {"browser_pdf_max_bytes": max_bytes}
    )
    assert limits == [max_bytes + 1, max_bytes - 4]
    assert session.disposed and page.aborted and page.cancelled
    assert not output.with_name(output.name + ".part").exists()
    if scenario == "complete":
        assert result and output.read_bytes() == payload
    else:
        assert not result and not output.exists()
        reason = (
            "PDF exceeds configured byte limit" if scenario == "oversized" else "Error"
        )
        assert f"body: {reason}; bytes=5;" in browser_engine.last_pdf_fetch_error()


# ============================================================
# Science server-signed ePDF transport
# ============================================================

SIGNED_PDFDIRECT = "/doi/pdfdirect/10.1126/adh2586?hmac=1790266406-QUJDREVGR0g%3D"


def test_science_reader_signed_url_accepts_only_site_signed_pdfdirect(monkeypatch):
    from scansci_pdf import science_transport as transport

    accepted = (
        SIGNED_PDFDIRECT,
        "/doi/pdfdirect/10.1126/x?hmac=1-abc",
    )
    rejected = (
        None,
        "/doi/pdf/10.1126/adh2586",
        "/doi/pdfdirect/10.1126/adh2586",
        "https://evil.example/doi/pdfdirect/10.1126/x?hmac=1-abc",
        "https://www.science.org.evil.example/doi/pdfdirect/10.1126/x?hmac=1-abc",
    )
    for value in accepted:
        monkeypatch.setattr(
            browser_engine, "evaluate_js", lambda *a, _v=value, **kw: _v
        )
        assert transport._science_reader_signed_url("tab", {}) == value
    for value in rejected:
        monkeypatch.setattr(
            browser_engine, "evaluate_js", lambda *a, _v=value, **kw: _v
        )
        assert transport._science_reader_signed_url("tab", {}) is None


def test_wait_for_science_reader_polls_until_reader_replaces_challenge(monkeypatch):
    from scansci_pdf import science_transport as transport

    answers = [None, None, SIGNED_PDFDIRECT]
    sleeps = []
    clock = [100.0]

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(transport.time, "sleep", sleep)
    monkeypatch.setattr(
        browser_engine,
        "evaluate_js",
        lambda *a, **kw: answers.pop(0) if answers else None,
    )
    assert transport._wait_for_science_reader("tab", {}, timeout=60) == SIGNED_PDFDIRECT
    assert sleeps == [2.0, 2.0]

    checks = []
    monkeypatch.setattr(
        browser_engine, "evaluate_js", lambda *a, **kw: checks.append(clock[0]) or None
    )
    sleeps.clear()
    assert transport._wait_for_science_reader("tab", {}, timeout=5) is None
    assert sleeps == [2.0, 2.0, 1.0]
    assert checks == [104.0, 106.0, 108.0, 109.0]
    checks.clear()
    sleeps.clear()
    assert transport._wait_for_science_reader("tab", {}, timeout=0) is None
    assert checks == [109.0]
    assert sleeps == []


def test_science_strategy_reader_budgets_and_default_hooks(monkeypatch):
    from scansci_pdf.publisher_strategies import science
    from scansci_pdf.publisher_strategies.sage import SAGEStrategy
    from scansci_pdf.publisher_strategies.science import ScienceStrategy

    budgets = []
    monkeypatch.setattr(
        science,
        "_wait_for_science_reader",
        lambda tab, config, *, timeout: budgets.append(timeout) or None,
    )
    strategy = ScienceStrategy()
    assert strategy.prepare_download_page("tab", "10.1126/adh2586", "<html/>", {}) == (
        "<html/>",
        [],
    )
    assert strategy.prepare_download_page(
        "tab", "10.1126/adh2586", "<title>Just a moment...</title>", {}
    ) == ("<title>Just a moment...</title>", [])
    assert budgets == [5.0, 60.0]
    sage = SAGEStrategy()
    assert sage.browser_entry_url("10.1177/example", {}) == sage.article_url(
        "10.1177/example"
    )
    assert sage.prepare_download_page("tab", "10.1177/example", "original", {}) == (
        "original",
        [],
    )


def _science_browser_stubs(monkeypatch, evaluate):
    from scansci_pdf import _publisher_strategies_core as core
    from scansci_pdf import science_transport as transport

    monkeypatch.setattr(browser_engine, "is_available", lambda config: True)
    monkeypatch.setattr(browser_engine, "create_tab", lambda *a, **kw: "own-tab")
    monkeypatch.setattr(browser_engine, "navigate_tab", lambda *a, **kw: True)
    monkeypatch.setattr(browser_engine, "close_tab", lambda *a: None)
    monkeypatch.setattr(browser_engine, "evaluate_js", evaluate)
    monkeypatch.setattr(core, "_inject_cookies_to_tab", lambda *a: None)
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    from scansci_pdf.publisher_strategies import science

    monkeypatch.setattr(
        science,
        "_wait_for_science_reader",
        lambda tab, cfg, **kw: transport._science_reader_signed_url(tab, cfg),
    )
    return core


def test_science_signed_pdfdirect_preferred_over_watermarked_pdf(
    monkeypatch, tmp_path, caplog
):
    fetched = []
    strategy = _science_browser_stubs(
        monkeypatch,
        lambda tab, js, config, **kw: (
            SIGNED_PDFDIRECT
            if "readerConfig" in js
            else "<html><body>Science reader</body></html>"
        ),
    )

    def fetch(tab, url, path, config):
        fetched.append(url)
        document = pymupdf.open()
        document.new_page()
        document.save(path)
        document.close()
        return True

    monkeypatch.setattr(browser_engine, "fetch_pdf_in_tab", fetch)
    with caplog.at_level(logging.INFO):
        result = strategy._browser_download(
            "10.1126/adh2586",
            "https://www.science.org/doi/epdf/10.1126/adh2586",
            tmp_path / "signed.pdf",
            {"browser_backend": "cdp", "interactive": False},
            "Science",
        )
    assert result and result["success"]
    assert fetched == [SIGNED_PDFDIRECT]
    # The short-lived signed URL must never reach logs.
    assert "hmac=" not in caplog.text
    assert "QUJDREVGR0g" not in caplog.text


def test_science_missing_reader_config_falls_back_to_watermarked_pdf(
    monkeypatch, tmp_path
):
    fetched = []
    strategy = _science_browser_stubs(
        monkeypatch,
        lambda tab, js, config, **kw: (
            None if "readerConfig" in js else "<html><body>Science reader</body></html>"
        ),
    )

    def fetch(tab, url, path, config):
        fetched.append(url)
        if url != "/doi/pdf/10.1126/adh2586":
            browser_engine._tls.pdf_fetch_reason = "no_pdf_found"
            return False
        document = pymupdf.open()
        document.new_page()
        document.save(path)
        document.close()
        return True

    monkeypatch.setattr(browser_engine, "fetch_pdf_in_tab", fetch)
    assert strategy._browser_download(
        "10.1126/adh2586",
        "https://www.science.org/doi/epdf/10.1126/adh2586",
        tmp_path / "fallback.pdf",
        {"browser_backend": "cdp", "interactive": False},
        "Science",
    )
    assert fetched == ["/doi/pdf/10.1126/adh2586"]


class FakeScienceTab:
    def __init__(self, url, *, scenario="success"):
        self.url = url
        self.scenario = scenario
        self.requests = []

    def evaluate(self, script, args):
        self.requests.append(args["url"])
        assert "same-origin" in script and "AbortSignal.timeout" in script
        path = args["url"].split("/doi/epdf/", 1)[1]
        html = (
            '{"epubConfig":{"epubUrl":"/doi/pdfdirect/' + path + '?hmac=fixture"}}'
            if self.scenario != "unsigned"
            else "<html>Reader not ready</html>"
        )
        return {"status": 200, "challenge": self.scenario == "challenge", "html": html}


def _science_test_pdf(path):
    with pymupdf.open() as document:
        for _ in range(7):
            document.new_page()
        document.save(path)


@pytest.mark.parametrize(
    "scenario", ["success", "challenge", "unsigned", "invalid_pdf"]
)
def test_science_fetch_reuses_owned_tab_and_falls_back(
    monkeypatch, tmp_path, caplog, scenario
):
    import requests

    from scansci_pdf import _publisher_strategies_core as core
    from scansci_pdf.publisher_strategies.science import ScienceStrategy

    def unexpected(*args, **kwargs):
        pytest.fail(
            "must not create/navigate/close the supplied tab or open a Python HTTP session"
        )

    tab = FakeScienceTab(
        "https://www.science.org/doi/epdf/10.1126/first", scenario=scenario
    )
    monkeypatch.setattr(browser_engine, "_resolve_tab", lambda tab_id: tab)
    monkeypatch.setattr(
        browser_engine, "get_owned_tab_for_origin", lambda origin: "owned"
    )
    for method in ("create_tab", "navigate_tab", "close_tab"):
        monkeypatch.setattr(browser_engine, method, unexpected)
    monkeypatch.setattr(requests, "Session", unexpected)
    downloads = []
    fallback = []

    def download(tab_id, url, path, cfg):
        downloads.append((tab_id, url))
        if scenario == "invalid_pdf":
            return False  # The real browser_engine rejects invalid PDF bytes.
        _science_test_pdf(path)
        return True

    def dom_download(doi, url, path, cfg, publisher):
        fallback.append((doi, url, publisher))
        _science_test_pdf(path)
        return True

    monkeypatch.setattr(browser_engine, "fetch_pdf_in_tab", download)
    monkeypatch.setattr(core, "_browser_download_with_fallback", dom_download)
    doi = "10.1126/next"
    reader = f"https://www.science.org/doi/epdf/{doi}"
    out = tmp_path / "paper.pdf"
    result = ScienceStrategy().download_sync(doi, out, {"browser_backend": "cdp"})
    assert result and out.is_file()
    assert tab.requests == [reader]
    if scenario == "success":
        assert result["source"] == "Science(Fetch)" and not fallback
    else:
        assert result["source"] == "Science(Browser)" and fallback == [
            (doi, reader, "Science")
        ]
    assert downloads == (
        [("owned", f"/doi/pdfdirect/{doi}?hmac=fixture")]
        if scenario in {"success", "invalid_pdf"}
        else []
    )
    assert "hmac=" not in caplog.text


@pytest.mark.parametrize("has_tab", [False, True])
def test_science_fetch_bootstraps_origin_once(monkeypatch, tmp_path, has_tab):
    from scansci_pdf import _publisher_strategies_core as core
    from scansci_pdf.publisher_strategies.science import ScienceStrategy

    tab = FakeScienceTab("about:blank")
    nav, opened, closed = [], [], []

    def create(url, config, *, timeout):
        opened.append(url)
        tab.url = url
        return "tab"

    def navigate(tab_id, url, config, *, timeout):
        nav.append(url)
        tab.url = url
        return True

    def fetch(tab_id, url, path, config):
        _science_test_pdf(path)
        return True

    monkeypatch.setattr(browser_engine, "_resolve_tab", lambda tab_id: tab)
    monkeypatch.setattr(browser_engine, "get_owned_tab_for_origin", lambda origin: None)
    monkeypatch.setattr(browser_engine, "create_tab", create)
    monkeypatch.setattr(browser_engine, "navigate_tab", navigate)
    monkeypatch.setattr(
        browser_engine, "close_tab", lambda tab_id, cfg: closed.append(tab_id)
    )
    monkeypatch.setattr(browser_engine, "fetch_pdf_in_tab", fetch)
    monkeypatch.setattr(
        core,
        "_browser_download_with_fallback",
        lambda *a: pytest.fail("unexpected fallback"),
    )
    doi = "10.1126/next"
    reader = f"https://www.science.org/doi/epdf/{doi}"
    result = ScienceStrategy().download_sync(
        doi,
        tmp_path / "paper.pdf",
        {"browser_backend": "cdp"},
        tab_id="tab" if has_tab else None,
    )
    assert result and result["source"] == "Science(Fetch)"
    assert tab.requests == [reader]
    assert (nav, opened, closed) == (
        ([reader], [], []) if has_tab else ([], [reader], ["tab"])
    )
