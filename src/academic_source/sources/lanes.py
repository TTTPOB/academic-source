"""Admission and worker ownership for paper source attempts."""

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import nullcontext
from queue import SimpleQueue
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import Any, TypeVar

from scansci_pdf.browser_engine import close_attempt_tabs, close_shared_browser

from .registry import ProviderSpec

log = logging.getLogger("academic_source.sources")
_T = TypeVar("_T")

# Launch their own authenticated browser/profile or use an external broker.
# Do not let a site override admit competing institutional login attempts.
_SERIAL_AUTH = {"CARSI", "WebVPN", "EZProxy", "SessionBroker", "InstitutionalBrowser"}


class ProviderLanes:
    def __init__(
        self, host_concurrency: dict[str, Any], *, browser_workers: int
    ) -> None:
        self._limits = host_concurrency
        self._browser_sites: dict[str, BoundedSemaphore] = {}
        self._http_sites: dict[str, BoundedSemaphore] = {}
        self._sites_lock = Lock()
        self._auth_lock = Lock()
        self._queue: SimpleQueue[Callable[[], None] | None] = SimpleQueue()
        self._prepared = Event()
        self._threads: list[Thread] = []
        self._browser_workers = browser_workers
        self._closed: Future[None] = Future()

    def start(self, prepare: Callable[[], None], close: Callable[[], None]) -> None:
        def worker(primary: bool) -> None:
            try:
                if primary:
                    try:
                        prepare()
                    except Exception as exc:  # noqa: BLE001 - preflight is best-effort
                        log.warning("Source preparation failed: %s", type(exc).__name__)
                    finally:
                        self._prepared.set()
                while (task := self._queue.get()) is not None:
                    task()
            finally:
                try:
                    if primary:
                        close()
                except BaseException as exc:  # noqa: BLE001 - forward worker errors
                    self._closed.set_exception(exc)
                else:
                    if primary:
                        self._closed.set_result(None)
                finally:
                    close_shared_browser()

        for index in range(self._browser_workers):
            thread = Thread(
                target=worker,
                args=(index == 0,),
                name=f"source-browser-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def wait_until_prepared(self) -> None:
        self._prepared.wait()

    def _site(
        self, sites: dict[str, BoundedSemaphore], key: str
    ) -> BoundedSemaphore | None:
        limit = int(self._limits.get(key, 1))
        if limit <= 0:
            return None
        with self._sites_lock:
            if key not in sites:
                sites[key] = BoundedSemaphore(limit)
            return sites[key]

    def browser(self, site: str, call: Callable[[], _T]) -> _T:
        queued = time.monotonic()
        future: Future[_T] = Future()

        def run() -> None:
            log.debug("browser queue wait seconds=%.3f", time.monotonic() - queued)
            try:
                try:
                    result = call()
                finally:
                    close_attempt_tabs()
            except BaseException as exc:  # noqa: BLE001 - forward worker errors
                future.set_exception(exc)
            else:
                future.set_result(result)

        # Wait on the paper worker, not in the browser pool: a busy site
        # should not take up browser threads that other sites could use.
        with self._site(self._browser_sites, site) or nullcontext():
            self._queue.put(run)
            return future.result()

    def attempt(self, label: str, spec: ProviderSpec, call: Callable[[], _T]) -> _T:
        match spec.backend:
            case "browser":
                if label in _SERIAL_AUTH:
                    with self._auth_lock:
                        return self.browser(spec.site_key, call)
                return self.browser(spec.site_key, call)
            case "http_ungated":
                with self._site(self._http_sites, spec.site_key) or nullcontext():
                    return call()
            case "http":
                return call()

    def shutdown(self) -> None:
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join()
        if self._threads:
            self._closed.result()
