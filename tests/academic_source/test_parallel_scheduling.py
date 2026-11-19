"""Test the parallel acquisition path without contacting an upstream server."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, get_ident
from urllib.parse import urlsplit

import pytest
import requests
from helpers import RecordingSource, isolate_upstreams, paper_bytes

from academic_source import sources
from academic_source.domain import AcquisitionRequest
from academic_source.interfaces.presentation import job_data
from academic_source.services.application import Application
from academic_source.settings import Settings
from academic_source.sources.registry import ProviderSpec
from scansci_pdf.network import fetch


def _application(tmp_path, source=None, *, workers=2, tabs=10, **config):
    return Application(
        Settings(
            data_dir=tmp_path,
            paper_workers=workers,
            browser_tabs=tabs,
            source_config={"scihub_enabled": False, **config},
        ),
        source=source,
    )


def _submit(app, *suffixes):
    return app.submit(
        AcquisitionRequest(
            identifiers=[f"10.1234/{suffix}" for suffix in suffixes], policy="grey_only"
        )
    )


def _provider(monkeypatch, label, backend, site, fn):
    monkeypatch.setitem(
        sources.PROVIDERS, label, ProviderSpec("oa", site, fn, backend=backend)
    )
    return label, fn


def test_scihub_requires_opt_out_before_parallel_startup(tmp_path):
    with pytest.raises(ValueError, match="source_config.scihub_enabled=false"):
        Application(Settings(data_dir=tmp_path, paper_workers=2))
    assert not (tmp_path / "catalog.sqlite").exists()


@pytest.mark.parametrize("mode", ["different_sites", "same_site", "override"])
@pytest.mark.parametrize("backend", ["http", "http_ungated"])
def test_parallel_http_limits(tmp_path, monkeypatch, mode, backend):
    isolate_upstreams(monkeypatch)
    first, second = Event(), Event()
    release = Event()
    content = paper_bytes()
    sites = [
        "one.example",
        "two.example" if mode == "different_sites" else "one.example",
    ]

    def transfer(index):
        (first, second)[index].set()
        assert release.wait(5)
        return content

    def send(_adapter, request, **_options):
        response = requests.Response()
        response.url = request.url
        response.status_code = 200
        response._content = transfer(int(urlsplit(request.url).path[-1]))
        response._content_consumed = True
        return response

    if backend == "http":
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)

    def provider(identifier, path, config):
        index = int(identifier[-1])
        if backend == "http_ungated":
            path.write_bytes(transfer(index))
        else:
            with fetch(
                f"https://{sites[index]}/{index}", config, stream=True
            ) as response:
                path.write_bytes(response.content)
        return {"file": path}

    handlers = [
        _provider(monkeypatch, f"Test{i}", backend, site, provider)
        for i, site in enumerate(sites)
    ]
    monkeypatch.setattr(sources, "_plan", lambda doi, *_: [handlers[int(doi[-1])]])
    limits = {"one.example": 2} if mode == "override" else {}
    app = _application(tmp_path, host_concurrency=limits)
    try:
        initial = _submit(app, "paper0")
        assert first.wait(5)
        other = _submit(app, "paper1")
        if mode == "same_site":
            assert not second.wait(0.05)
        else:
            assert second.wait(5)
        release.set()
        assert app.wait(initial.id, 5).status == "succeeded"
        assert app.wait(other.id, 5).status == "succeeded"
    finally:
        release.set()
        app.close()


def test_failed_stream_releases_redirect_target_slot(tmp_path, monkeypatch):
    isolate_upstreams(monkeypatch)
    content = paper_bytes()
    events = []

    class FailedStream:
        def stream(self, *_args, **_kwargs):
            raise requests.ConnectionError("synthetic interrupted stream")
            yield  # pragma: no cover

        def close(self):
            events.append("closed")

    def send(_adapter, request, **_options):
        response = requests.Response()
        response.url = request.url
        host = urlsplit(request.url).hostname
        events.append(host)
        response.status_code = 200
        if host == "doi.example":
            response.status_code = 302
            response.headers["Location"] = "https://cdn.example/broken"
            response._content = b""
        elif request.url.endswith("broken"):
            response.raw = FailedStream()
            return response
        else:
            response._content = content
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)

    def provider(identifier, path, config):
        url = (
            "https://doi.example/redirect"
            if identifier.endswith("fail")
            else "https://cdn.example/healthy"
        )
        with fetch(url, config, stream=True) as response, path.open("wb") as file:
            for chunk in response.iter_content(4096):
                file.write(chunk)
        return {"file": path}

    handler = _provider(monkeypatch, "TestHTTP", "http", "doi.example", provider)
    monkeypatch.setattr(sources, "_plan", lambda *_: [handler])
    app = _application(tmp_path)
    try:
        fail = _submit(app, "fail")
        assert app.wait(fail.id, 5).status == "failed"
        success = _submit(app, "success")
        assert app.wait(success.id, 5).status == "succeeded"
        assert events == ["doi.example", "cdn.example", "closed", "cdn.example"]
    finally:
        app.close()


def test_browser_attempts_serialize_on_one_thread_while_http_overlaps(
    tmp_path, monkeypatch
):
    http_barrier, lock = Barrier(2, timeout=5), Lock()
    browser_entered, release_browser = Event(), Event()
    http_threads: set[int] = set()
    calls = []
    content = paper_bytes()
    active_browser = 0

    def http(_identifier, _path, _config):
        with lock:
            http_threads.add(get_ident())
        http_barrier.wait()

    def browser(identifier, path, _config):
        nonlocal active_browser
        with lock:
            active_browser += 1
            assert active_browser == 1
        calls.append(("browser", get_ident()))
        browser_entered.set()
        assert release_browser.wait(5)
        path.write_bytes(content)
        with lock:
            active_browser -= 1
        return {"file": path}

    http_handler = _provider(monkeypatch, "TestHttp", "http", "one.example", http)
    browser_handlers = {
        "10.1234/one": _provider(
            monkeypatch, "TestOne", "browser", "one.example", browser
        ),
        "10.1234/two": _provider(
            monkeypatch, "TestTwo", "browser", "two.example", browser
        ),
    }
    monkeypatch.setattr(
        sources, "_plan", lambda doi, *_: [http_handler, browser_handlers[doi]]
    )
    monkeypatch.setattr(
        sources.LegacySources,
        "prepare",
        lambda *_: calls.append(("prepare", get_ident())),
    )
    monkeypatch.setattr(
        sources.LegacySources, "close", lambda *_: calls.append(("close", get_ident()))
    )
    app = _application(tmp_path)
    try:
        first, second = _submit(app, "one"), _submit(app, "two")
        assert browser_entered.wait(5)
        assert len(http_threads) == 2
        release_browser.set()
        assert app.wait(first.id, 5).status == "succeeded"
        assert app.wait(second.id, 5).status == "succeeded"
        assert [action for action, _ in calls] == ["prepare", "browser", "browser"]
    finally:
        release_browser.set()
        app.close()
    assert [action for action, _ in calls] == ["prepare", "browser", "browser", "close"]
    threads = {thread for _, thread in calls}
    assert len(threads) == 1 and not (threads & http_threads)


@pytest.mark.parametrize(
    ("names", "sites", "host_concurrency", "tabs", "maximum"),
    [
        (("one", "two"), ("a.example", "b.example"), {}, 10, 2),
        (("one", "two"), ("a.example", "a.example"), {}, 10, 1),
        (("one", "two"), ("a.example", "a.example"), {"a.example": 2}, 10, 2),
        (("one", "two", "three"), ("a.example", "b.example", "c.example"), {}, 2, 2),
    ],
)
def test_cdp_browser_site_and_tab_limits(
    tmp_path, monkeypatch, names, sites, host_concurrency, tabs, maximum
):
    from scansci_pdf import browser_backend

    monkeypatch.setattr(browser_backend, "probe_cdp", lambda _config: None)
    ready_for_browser = Barrier(len(names), timeout=5)
    full, release = Event(), Event()
    calls, active, peak = [], 0, 0
    lock, content = Lock(), paper_bytes()

    def http(*_):
        ready_for_browser.wait()

    def browser(_identifier, path, _config):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
            calls.append(get_ident())
            if active == maximum:
                full.set()
        try:
            assert release.wait(5)
            path.write_bytes(content)
            return {"file": path}
        finally:
            with lock:
                active -= 1

    http_handler = _provider(monkeypatch, "TestHttp", "http", "a.example", http)
    browser_handlers = {
        f"10.1234/{name}": _provider(
            monkeypatch, f"Browser{index}", "browser", site, browser
        )
        for index, (name, site) in enumerate(zip(names, sites, strict=True))
    }
    monkeypatch.setattr(
        sources, "_plan", lambda doi, *_: [http_handler, browser_handlers[doi]]
    )
    app = _application(
        tmp_path,
        workers=len(names),
        tabs=tabs,
        browser_backend="cdp",
        host_concurrency=host_concurrency,
    )
    try:
        job = _submit(app, *names)
        assert full.wait(5)
        if maximum < len(names):
            # The barrier puts all papers at browser admission before we release the active calls.
            assert not Event().wait(0.05)
            assert len(calls) == maximum
        release.set()
        assert app.wait(job.id, 5).status == "succeeded"
        assert len(calls) == len(names)
        assert peak == maximum
    finally:
        release.set()
        app.close()


def test_cdp_errors_tabs_and_connections_belong_to_each_worker(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scansci_pdf import _publisher_strategies_core as diagnostics
    from scansci_pdf import browser_backend, browser_engine

    monkeypatch.setattr(browser_backend, "probe_cdp", lambda _config: None)
    # Exercise the real engine and borrowed-session ownership; only Chrome is fake.
    tabs, tabs_closed, sessions_closed, registered = {}, [], [], {}
    reached, release = Event(), Event()
    error_set, clean_read = Event(), Event()
    both_adapters = Barrier(2, timeout=5)
    content = paper_bytes()
    attached = set()

    class Page:
        def __init__(self):
            self.owner = get_ident()
            self.callback = None

        def on(self, _event, callback):
            self.callback = callback

        def goto(self, url, **_kwargs):
            assert get_ident() == self.owner
            self.url = url

        def close(self):
            assert get_ident() == self.owner
            tabs_closed.append(self.owner)
            self.callback(self)

    def connect(config):
        owner = get_ident()
        attached.add(owner)
        registry = browser_backend.OwnedTargets(config, "local")

        def disconnect():
            assert get_ident() == owner
            sessions_closed.append(owner)

        def cannot_recover_active_tab():
            pytest.fail("another worker's active tabs are not stale")

        browser = SimpleNamespace(
            is_connected=lambda: True,
            close=disconnect,
            new_browser_cdp_session=cannot_recover_active_tab,
        )
        registry.recover(browser)
        context = SimpleNamespace(
            new_page=Page,
            new_cdp_session=lambda page: SimpleNamespace(
                send=lambda _method: {"targetInfo": {"targetId": str(page.owner)}},
                detach=lambda: None,
            ),
        )
        return browser_backend.BorrowedCDPSession(
            browser, context, SimpleNamespace(stop=lambda: None), registry
        )

    monkeypatch.setattr(browser_engine, "connect_cdp", connect)

    def blocked(_identifier, _path, config):
        both_adapters.wait()
        tab = browser_engine.create_tab("https://a.example/", config)
        tabs["blocked"] = tab
        registered["blocked"] = get_ident()
        diagnostics._set_error("cloudflare_blocked", "try_other_source")
        error_set.set()
        assert clean_read.wait(5)
        reached.set()
        assert release.wait(5)

    def works(_identifier, path, config):
        both_adapters.wait()
        assert error_set.wait(5)
        tab = browser_engine.create_tab("https://b.example/", config)
        assert browser_engine._resolve_tab(tabs["blocked"]) is None
        assert browser_engine.get_owned_tab_for_origin("https://b.example/") == tab
        registered["works"] = get_ident()
        assert diagnostics.get_last_error() == ("", "")
        clean_read.set()
        assert release.wait(5)
        path.write_bytes(content)
        return {"file": path}

    # Let the second legacy adapter clear its own slot before the first writes,
    # then read both slots while the first attempt is still running.
    handlers = {
        "10.1234/blocked": _provider(
            monkeypatch, "OneBrowser", "browser", "a.example", blocked
        ),
        "10.1234/works": _provider(
            monkeypatch, "TwoBrowser", "browser", "b.example", works
        ),
    }
    monkeypatch.setattr(sources, "_plan", lambda doi, *_: [handlers[doi]])
    app = _application(tmp_path, tabs=2, browser_backend="cdp")
    with ThreadPoolExecutor(1) as ex:
        try:
            job = _submit(app, "blocked", "works")
            assert reached.wait(5)
            closing = ex.submit(app.close)
            assert not closing.done()
            assert not tabs_closed and not sessions_closed
        finally:
            release.set()
            ex.submit(app.close).result(timeout=5)
        closing.result(timeout=5)
    job = app.job(job.id)
    assert job.results[0].attempts[0].reason == "cloudflare_blocked"
    assert job.results[1].status == "succeeded"
    assert len(attached) == 2
    assert (
        attached == set(registered.values()) == set(tabs_closed) == set(sessions_closed)
    )
    assert len(tabs_closed) == len(sessions_closed) == 2


def test_oa_browser_fallback_uses_candidate_site_and_browser_thread(
    tmp_path, monkeypatch
):
    from academic_source.sources import oa
    from academic_source.sources.errors import Blocked
    from academic_source.sources.models import SourceSuccess
    from scansci_pdf import browser_backend

    monkeypatch.setattr(browser_backend, "probe_cdp", lambda _config: None)
    url = "https://repository.example/paper"
    monkeypatch.setattr(
        oa, "discover", lambda *_: iter([oa.Discovery("OA", [oa.Candidate(url, "OA")])])
    )
    events = []
    content = paper_bytes()

    def http(*_):
        events.append(("http", get_ident()))
        return Blocked(), []

    def browser(candidate, path, _config):
        events.append(("browser", get_ident()))
        path.write_bytes(content)
        return SourceSuccess(path=path, source=candidate.source, url=candidate.url)

    monkeypatch.setattr(oa, "_http_candidate", http)
    monkeypatch.setattr(oa, "_browser_candidate", browser)
    app = _application(tmp_path, browser_backend="cdp")
    assert app._lanes is not None
    route = app._lanes.browser

    def record_route(site, call):
        events.append((site, get_ident()))
        return route(site, call)

    monkeypatch.setattr(app._lanes, "browser", record_route)
    try:
        job = app.submit(
            AcquisitionRequest(identifiers=["10.1234/paper"], policy="oa_first")
        )
        assert app.wait(job.id, 5).status == "succeeded"
        assert [label for label, _ in events] == [
            "http",
            "repository.example",
            "browser",
        ]
        assert events[0][1] == events[1][1] != events[2][1]
    finally:
        app.close()


class HeldSource(RecordingSource):
    def __init__(self, *blocked):
        super().__init__()
        self.started = {name: Event() for name in blocked}
        self.release = {name: Event() for name in blocked}
        self.closed = Event()

    def acquire(self, identifier, request, work_dir, config):
        name = identifier.split("/")[1]
        if name in self.started:
            self.started[name].set()
            assert self.release[name].wait(5)
        return super().acquire(identifier, request, work_dir, config)

    def close(self):
        self.closed.set()

    def release_all(self):
        for event in self.release.values():
            event.set()


def test_out_of_order_completion_is_saved_in_submission_order(tmp_path):
    source = HeldSource("first", "third")
    app = _application(tmp_path, source)
    try:
        job = _submit(app, "first", "second", "third")
        assert source.started["third"].wait(5)
        running = app.job(job.id)
        saved = app.store.get_job(job.id)
        assert running.completed == saved.completed == 1
        assert [result.identifier for result in saved.results] == ["10.1234/second"]
        assert job_data(running)["progress"] == {
            "current": 1,
            "total": 3,
            "identifier": "10.1234/first",
        }
        source.release_all()
        done = app.wait(job.id, 5)
        assert done.status == "succeeded" and done.completed == 3
        assert done == app.store.get_job(job.id)
        assert [result.identifier for result in done.results] == [
            "10.1234/first",
            "10.1234/second",
            "10.1234/third",
        ]
    finally:
        source.release_all()
        app.close()


def test_parallel_cancellation_and_shutdown_drain_accepted_papers(
    tmp_path, monkeypatch
):
    source = HeldSource("first", "second", "tail")
    app = _application(tmp_path, source)
    closing = Event()
    clear = app.downloads.clear

    def signal_closing():
        clear()
        closing.set()

    monkeypatch.setattr(app.downloads, "clear", signal_closing)
    with ThreadPoolExecutor(1) as callers:
        try:
            running = _submit(app, "first", "second", "skip")
            assert source.started["first"].wait(5) and source.started["second"].wait(5)
            queued = _submit(app, "queued-skip")
            tail = _submit(app, "tail", "rest")
            assert job_data(tail)["queue"] == {"ahead": 2}
            assert app.cancel(queued.id).status == "cancelled"
            assert job_data(app.job(tail.id))["queue"] == {"ahead": 1}
            assert app.cancel(running.id).status == "running"
            future = callers.submit(app.close)
            assert closing.wait(5)
            with pytest.raises(RuntimeError, match="closed"):
                _submit(app, "new")
            source.release["second"].set()
            assert source.started["tail"].wait(5)
            assert not future.done() and not source.closed.is_set()
            assert app.job(running.id).completed == 1
            source.release_all()
            future.result(timeout=5)
            cancelled = app.job(running.id)
            assert cancelled.status == "cancelled" and cancelled.completed == 2
            assert [r.identifier for r in cancelled.results] == [
                "10.1234/first",
                "10.1234/second",
            ]
            assert app.job(tail.id).status == "succeeded"
            assert set(source.calls) == {
                "10.1234/first",
                "10.1234/second",
                "10.1234/tail",
                "10.1234/rest",
            }
            assert source.closed.is_set()
        finally:
            source.release_all()
            app.close()
