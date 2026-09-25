"""CDP lifecycle and page-origin byte transport; no real browser is launched."""

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

    def evaluate(self, script):
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
        data = self.page.payload[self.position : self.position + 32768]
        self.position += len(data)
        return {"done": not data, "bytes": list(data)}

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


class FakeScienceResponse:
    def __init__(self, payload, content_type, status=200, extra_headers=None):
        self.status_code = status
        self.headers = {"content-type": content_type, **(extra_headers or {})}
        self.text = payload.decode("utf-8", "replace")
        self._payload = payload
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        yield self._payload


class FakeScienceSession:
    """requests.Session stand-in for the plain-HTTP Science transport."""

    def __init__(self, html, pdf_bytes=b"", html_headers=None):
        self.html = html
        self.pdf_bytes = pdf_bytes
        self.html_headers = html_headers or {}
        self.urls = []
        self.responses = []
        self.closed = False

    def close(self):
        self.closed = True

    def get(self, url, **kwargs):
        self.urls.append(url)
        if kwargs.get("stream"):
            response = FakeScienceResponse(self.pdf_bytes, "application/pdf")
        else:
            response = FakeScienceResponse(
                self.html.encode(), "text/html", extra_headers=self.html_headers
            )
        self.responses.append(response)
        return response


def _pdf_bytes():
    """A payload large enough to satisfy is_pdf_file's size floor."""
    document = pymupdf.open()
    for index in range(3):
        page = document.new_page()
        page.insert_text((72, 72), f"Signed Science fixture page {index} " * 40)
    payload = document.tobytes()
    document.close()
    return payload


def test_science_plain_http_transport_resolves_and_validates(
    monkeypatch, tmp_path, caplog
):
    from scansci_pdf import science_transport as transport

    signed = "/doi/pdfdirect/10.1126/adh2586?hmac=1790266406-QUJDREVGR0g%3D"
    payload = _pdf_bytes()
    session = FakeScienceSession('{"epubConfig":{"epubUrl":"' + signed + '"}}', payload)
    monkeypatch.setattr(
        transport, "_science_http_session", lambda config, state: session
    )

    output = tmp_path / "http.pdf"
    assert transport._science_http_download("10.1126/adh2586", output, {}, {})
    assert output.read_bytes() == payload
    assert session.urls[-1].endswith(signed)
    assert session.closed and all(response.closed for response in session.responses)

    # A challenge response must be vetoed by its mitigation header even when the
    # body happens to carry a reader-looking payload.
    challenged = FakeScienceSession(
        '{"epubConfig":{"epubUrl":"' + signed + '"}}',
        payload,
        {"cf-mitigated": "challenge"},
    )
    monkeypatch.setattr(
        transport, "_science_http_session", lambda config, state: challenged
    )
    assert not transport._science_http_download(
        "10.1126/adh2586", tmp_path / "a.pdf", {}, {}
    )
    assert challenged.closed and all(
        response.closed for response in challenged.responses
    )
    assert "HTTP reader challenge" in caplog.text
    assert "HTTP reader status=" not in caplog.text
    caplog.clear()

    unsigned = FakeScienceSession(
        '{"epubConfig":{"epubUrl":"/doi/pdf/10.1126/x"}}', payload
    )
    monkeypatch.setattr(
        transport, "_science_http_session", lambda config, state: unsigned
    )
    assert not transport._science_http_download(
        "10.1126/adh2586", tmp_path / "b.pdf", {}, {}
    )
    assert "HTTP reader unsupported epub URL" in caplog.text
    assert "HTTP reader challenge" not in caplog.text
    assert "hmac=" not in caplog.text
    assert "QUJDREVGR0g" not in caplog.text


def test_science_clearance_capture_enables_and_gates_the_fast_path(
    monkeypatch, tmp_path
):
    from scansci_pdf import browser_cookies
    from scansci_pdf import science_transport as transport

    config = {"cache_dir": str(tmp_path)}
    monkeypatch.setattr(
        browser_engine, "evaluate_js", lambda tab, js, config, **kw: "Agent/1.0 Chrome"
    )
    monkeypatch.setattr(
        browser_engine,
        "context_cookies",
        lambda url, config: [
            {
                "name": "cf_clearance",
                "value": "token",
                "domain": ".www.science.org",
                "path": "/",
            },
            {"name": "tracker", "value": "x", "domain": ".example.com", "path": "/"},
        ],
    )
    assert browser_cookies.load_science_http_state(config) is None
    transport._capture_science_clearance("tab", config)
    assert (
        browser_cookies.load_science_http_state(config)["user_agent"]
        == "Agent/1.0 Chrome"
    )
    assert [c["name"] for c in browser_cookies.load_saved_cookies(config)] == [
        "cf_clearance"
    ]


@pytest.mark.parametrize(
    "backend,override,expected,browser_proxy",
    [
        ("patchright", None, "http://browser:8080", "http://browser:8080"),
        ("patchright", None, "http://network:8080", "   "),
        ("cdp", None, "http://global:8080", "http://browser:8080"),
        ("patchright", "", None, "http://browser:8080"),
        ("cdp", "http://explicit:8080", "http://explicit:8080", "http://browser:8080"),
    ],
)
def test_science_session_proxy_selection(
    monkeypatch, backend, override, expected, browser_proxy
):
    from scansci_pdf import science_transport as transport

    monkeypatch.setenv("SCANSCI_PDF_PROXY", "http://global:8080")
    config = {
        "browser_backend": backend,
        "browser_static_proxy": browser_proxy,
        "network_proxy": "http://network:8080",
        "science_http_proxy": override,
    }
    state = {"user_agent": "Agent/paired", "cookies": []}
    session = transport._science_http_session(config, state)
    try:
        assert session.trust_env is False
        assert session.headers["User-Agent"] == "Agent/paired"
        assert session.proxies == (
            {"http": expected, "https": expected} if expected else {}
        )
    finally:
        session.close()


def test_science_snapshot_rejects_wrong_scope_and_expired_clearance(
    monkeypatch, tmp_path, caplog
):
    from scansci_pdf import browser_cookies

    monkeypatch.setattr(browser_cookies.time, "time", lambda: 100.0)
    config = {"cache_dir": str(tmp_path)}
    clearance = {
        "name": "cf_clearance",
        "value": "token",
        "domain": ".science.org",
        "path": "/",
        "expires": -1,
    }
    browser_cookies.save_science_http_state("Agent/2", [clearance], config)
    state = browser_cookies.load_science_http_state(config)
    assert state["user_agent"] == "Agent/2" and state["cookies"][0]["expires"] == -1
    for bad in (
        {**clearance, "domain": ".evilscience.org"},
        {**clearance, "path": "/doi/epdf"},
        {**clearance, "expires": 1},
    ):
        browser_cookies.save_science_http_state("Agent/3", [bad], config)
        assert (
            browser_cookies.load_science_http_state(config)["user_agent"] == "Agent/2"
        )
    browser_cookies.save_science_http_state(
        "Agent/4", [{**clearance, "expires": 200}], config
    )
    assert browser_cookies.load_science_http_state(config)["user_agent"] == "Agent/4"
    monkeypatch.setattr(browser_cookies.time, "time", lambda: 201.0)
    assert browser_cookies.load_science_http_state(config) is None
    assert "clearance expired or invalid" in caplog.text
    (tmp_path / browser_cookies.SCIENCE_HTTP_STATE_FILE).unlink()
    caplog.clear()
    assert browser_cookies.load_science_http_state(config) is None
    assert "snapshot missing" in caplog.text


def test_science_fast_path_skips_the_browser(monkeypatch, tmp_path):
    from scansci_pdf import _publisher_strategies_core as core
    from scansci_pdf import browser_cookies

    monkeypatch.setattr(
        browser_cookies,
        "load_science_http_state",
        lambda config: {"user_agent": "Agent", "cookies": []},
    )
    monkeypatch.setattr(
        browser_engine,
        "create_tab",
        lambda *a, **kw: pytest.fail("browser must not open"),
    )

    def http_download(doi, path, config, state):
        path.write_bytes(_pdf_bytes())
        return True

    from scansci_pdf.publisher_strategies import science

    monkeypatch.setattr(science, "_science_http_download", http_download)
    result = core.try_science_browser(
        "10.1126/adh2586", tmp_path / "fast.pdf", {"browser_backend": "cdp"}
    )
    assert result and result["success"]
    assert result["source"] == "Science(Signed)"


def test_science_without_clearance_still_uses_the_browser(monkeypatch, tmp_path):
    from scansci_pdf import _publisher_strategies_core as core
    from scansci_pdf import browser_cookies

    monkeypatch.setattr(browser_cookies, "load_science_http_state", lambda config: None)
    from scansci_pdf.publisher_strategies import science

    monkeypatch.setattr(
        science,
        "_science_http_download",
        lambda *a: pytest.fail("fast path must not run"),
    )
    monkeypatch.setattr(browser_engine, "is_available", lambda config: True)
    monkeypatch.setattr(browser_engine, "create_tab", lambda *a, **kw: None)
    assert (
        core.try_science_browser(
            "10.1126/adh2586", tmp_path / "slow.pdf", {"browser_backend": "cdp"}
        )
        is None
    )
