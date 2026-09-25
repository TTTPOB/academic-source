"""Offline checks for CDP target ownership and recovery."""

import json
from types import SimpleNamespace

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
