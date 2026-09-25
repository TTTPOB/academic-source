"""Offline checks for CDP target ownership and recovery."""

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from scansci_pdf import browser_backend


class Page:
    def __init__(self):
        self.callback = None
        self.closed = False

    def on(self, event, callback):
        assert event == "close"
        self.callback = callback

    def close(self):
        self.closed = True
        self.callback(self)


def test_created_target_is_recorded_and_normal_close_removes_it(tmp_path):
    registry = browser_backend.OwnedTargets(
        {"cache_dir": str(tmp_path)}, "http://localhost:9222"
    )
    page = Page()
    cdp = SimpleNamespace(
        send=lambda method: {"targetInfo": {"targetId": "owned-id"}},
        detach=lambda: None,
    )
    context = SimpleNamespace(new_page=lambda: page, new_cdp_session=lambda p: cdp)
    session = browser_backend.BorrowedCDPSession(None, context, None, registry)
    assert session.new_page() is page
    assert json.loads(registry.path.read_text()) == {
        "http://localhost:9222": ["owned-id"]
    }
    session.close_page(page)
    assert page.closed
    assert registry.load() == {}


@pytest.mark.parametrize("failure", ["target_lookup", "registry_write"])
def test_new_page_failure_closes_created_page(monkeypatch, tmp_path, failure):
    registry = browser_backend.OwnedTargets(
        {"cache_dir": str(tmp_path)}, "http://localhost:9222"
    )
    page = Page()

    def target_info(method):
        if failure == "target_lookup":
            raise RuntimeError("lookup failed")
        return {"targetInfo": {"targetId": "owned-id"}}

    if failure == "registry_write":
        monkeypatch.setattr(
            registry,
            "add",
            lambda target_id: (_ for _ in ()).throw(OSError("disk full")),
        )
    cdp = SimpleNamespace(send=target_info, detach=lambda: None)
    context = SimpleNamespace(new_page=lambda: page, new_cdp_session=lambda p: cdp)
    session = browser_backend.BorrowedCDPSession(None, context, None, registry)
    with pytest.raises((RuntimeError, OSError)):
        session.new_page()
    assert page.closed
    assert page not in session.pages


def test_close_event_and_failed_close_preserve_record(tmp_path):
    registry = browser_backend.OwnedTargets(
        {"cache_dir": str(tmp_path)}, "http://localhost:9222"
    )
    page = Page()
    cdp = SimpleNamespace(
        send=lambda method: {"targetInfo": {"targetId": "owned-id"}},
        detach=lambda: None,
    )
    context = SimpleNamespace(new_page=lambda: page, new_cdp_session=lambda p: cdp)
    session = browser_backend.BorrowedCDPSession(None, context, None, registry)
    session.new_page()

    def disconnected_close():
        page.callback(page)
        raise RuntimeError("connection lost")

    page.close = disconnected_close
    with pytest.raises(RuntimeError, match="connection lost"):
        session.close_page(page)
    assert registry.load()[registry.endpoint] == ["owned-id"]
    assert page not in session.pages


def test_reconnect_only_closes_recorded_targets_and_retries_failures(
    monkeypatch, tmp_path
):
    url = "http://localhost:9222"
    registry = browser_backend.OwnedTargets({"cache_dir": str(tmp_path)}, url)
    registry.add("owned-id")
    registry.add("stale-id")
    actions = []
    fail = [True]

    def send(method, params=None):
        actions.append((method, params))
        if method == "Target.getTargets":
            return {
                "targetInfos": [{"targetId": "owned-id"}, {"targetId": "external-id"}]
            }
        if fail[0]:
            raise RuntimeError("secret endpoint must not be logged")
        return {"success": True}

    cdp = SimpleNamespace(send=send, detach=lambda: None)
    browser = SimpleNamespace(
        contexts=[SimpleNamespace()],
        new_browser_cdp_session=lambda: cdp,
        close=lambda: None,
    )
    driver = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=lambda *args, **kwargs: browser),
        stop=lambda: None,
    )
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: SimpleNamespace(start=lambda: driver)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    config = {"cache_dir": str(tmp_path), "browser_cdp_url": url}

    browser_backend.probe_cdp(config)
    assert actions == []
    assert registry.load()[url] == ["owned-id", "stale-id"]

    browser_backend.connect_cdp(config).close()
    assert registry.load()[url] == ["owned-id"]
    assert ("Target.closeTarget", {"targetId": "owned-id"}) in actions
    assert ("Target.closeTarget", {"targetId": "external-id"}) not in actions
    fail[0] = False
    browser_backend.connect_cdp(config).close()
    assert registry.load() == {}
    assert actions.count(("Target.closeTarget", {"targetId": "owned-id"})) == 2
