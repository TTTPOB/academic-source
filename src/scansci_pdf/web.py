"""Compatibility ASGI entrypoint for the unified academic-source service.

Modified by academic-source: the former independent Web business logic now
lives in shared application services. Prefer academic-source serve.
"""

from academic_source.app import create_app


def __getattr__(name: str):
    if name != "app":
        raise AttributeError(name)
    app = create_app()
    globals()["app"] = app
    return app
