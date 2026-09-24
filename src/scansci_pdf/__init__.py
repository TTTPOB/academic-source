"""Legacy ScanSci source library retained by academic-source.

Modified by academic-source: importing adapters no longer mutates process-wide
proxy or TLS settings. Public convenience exports are loaded on demand.
"""

from importlib import import_module

__version__ = "1.17.0"

_EXPORTS = {
    "download": ".sources",
    "batch_download": ".sources",
    "search_papers": ".search",
    "load_config": ".config",
    "update_config": ".config",
    "get_config_safe": ".config",
}
__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
