"""Publisher and acquisition source adapters.

The old batch scheduler is imported only for legacy parsing paths that still
refer to it; importing individual provider modules does not initialize it.
"""

from __future__ import annotations


def __getattr__(name: str):
    if name in {"download", "batch_download", "_write_download_results"}:
        from . import _legacy

        return getattr(_legacy, name)
    raise AttributeError(name)
