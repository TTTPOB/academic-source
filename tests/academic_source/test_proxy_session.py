"""An authenticated libproxy article URL must not be mistaken for a login page."""

from types import SimpleNamespace

from tests.academic_source.helpers import paper_bytes


def test_ezproxy_reuses_headless_session_and_reports_actual_login(
    tmp_path, monkeypatch
):
    from scansci_pdf import browser_backend, browser_engine
    from scansci_pdf.sources import ezproxy

    payload = paper_bytes() + b"\n" * 6000
    launched = []

    class Browser:
        target = "https://publisher.libproxy.example/doi/article"
        url = ""

        def new_context(self):
            return self

        def new_page(self):
            return self

        def on(self, event, callback):
            self.capture = callback

        def goto(self, *args, **kwargs):
            self.url = self.target
            if self.target.endswith("/article"):
                self.capture(
                    SimpleNamespace(
                        headers={"content-type": "application/pdf"},
                        body=lambda: payload,
                    )
                )

        def close(self):
            pass

    browser = Browser()
    monkeypatch.setattr(
        ezproxy.requests,
        "head",
        lambda *args, **kwargs: SimpleNamespace(
            url="https://publisher.example/article"
        ),
    )
    monkeypatch.setattr(ezproxy.time, "sleep", lambda _: None)
    monkeypatch.setattr(browser_backend, "is_available", lambda *args: True)
    monkeypatch.setattr(
        browser_backend, "launch", lambda **kwargs: launched.append(kwargs) or browser
    )
    monkeypatch.setattr(browser_engine, "_build_browser_args", lambda config: [])
    config = {
        "ezproxy_enabled": True,
        "ezproxy_login_url": "https://libproxy.example/login?url={url}",
        "interactive": False,
        "cache_dir": str(tmp_path),
    }
    path = tmp_path / "paper.pdf"
    result = ezproxy.try_ezproxy("10.1234/example", path, config)
    assert result["success"] is True
    assert path.read_bytes() == payload
    browser.target = "https://libproxy.example/login"
    blocked = ezproxy.try_ezproxy("10.1234/other", tmp_path / "other.pdf", config)
    assert blocked["success"] is False and blocked["error_type"] == "auth_required"
    assert all(
        options["headless"] and options["config"] == config for options in launched
    )
