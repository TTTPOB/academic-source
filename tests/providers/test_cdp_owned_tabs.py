"""Offline checks for CDP target ownership and recovery."""

import json
import sys
from types import ModuleType, SimpleNamespace

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
