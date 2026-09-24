"""CDP lifecycle and page-origin byte transport; no real browser is launched."""

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
