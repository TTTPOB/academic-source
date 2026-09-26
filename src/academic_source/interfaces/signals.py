"""CLI-only process shutdown signals."""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager


@contextmanager
def graceful_signals(stop: Callable[[], None]) -> Iterator[None]:
    """Request a drain once; a second SIGINT exits without joining worker threads."""
    interrupted = False
    stopping = False
    original = {}

    def handle(signum: int, _frame: object) -> None:
        nonlocal interrupted, stopping
        if signum == signal.SIGINT:
            if interrupted:
                os._exit(128 + signal.SIGINT)
            interrupted = True
            print(
                "Draining accepted jobs; press Ctrl+C again to force exit.",
                file=sys.stderr,
                flush=True,
            )
        if not stopping:
            stopping = True
            stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        original[signum] = signal.signal(signum, handle)
    try:
        yield
    finally:
        for signum, handler in original.items():
            signal.signal(signum, handler)
