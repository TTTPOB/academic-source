"""Single-process, single-use capabilities for registered artifact files."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from academic_source.domain import Artifact
from academic_source.infrastructure.storage import Store

_MAX_TTL_SECONDS = 6 * 60 * 60


class DownloadCapabilities:
    def __init__(
        self,
        store: Store,
        *,
        ttl_seconds: float = _MAX_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0 < ttl_seconds <= _MAX_TTL_SECONDS:
            raise ValueError("Download lifetime must be positive and at most six hours")
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._tokens: dict[str, tuple[str, float]] = {}
        self._artifacts: dict[str, str] = {}
        # MCP workers and HTTP downloads can issue/claim concurrently.
        self._lock = Lock()

    def issue(self, artifact_id: str, *, base_url: str) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            for token, (_, expires_at) in list(self._tokens.items()):
                if expires_at <= now:
                    self._remove(token)
            self._file(artifact_id)
            token = self._artifacts.get(artifact_id)
            if token is None:
                token = secrets.token_urlsafe(32)
                self._tokens[token] = (artifact_id, now + self.ttl_seconds)
                self._artifacts[artifact_id] = token
            _, expires_at = self._tokens[token]
            return {
                "download_url": f"{base_url.rstrip('/')}/downloads/{token}",
                "download_expires_at": datetime.fromtimestamp(
                    expires_at, UTC
                ).isoformat(),
                "download_single_use": True,
            }

    def claim(self, token: str) -> tuple[Artifact, Path]:
        with self._lock:
            artifact_id, expires_at = self._tokens[token]
            if expires_at <= self.clock():
                self._remove(token)
                raise KeyError(token)
            artifact, path = self._file(artifact_id)
            # Consume before streaming, even if the client later disconnects.
            self._remove(token)
            return artifact, path

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._artifacts.clear()

    def _remove(self, token: str) -> None:
        artifact_id, _ = self._tokens.pop(token)
        self._artifacts.pop(artifact_id)

    def _file(self, artifact_id: str) -> tuple[Artifact, Path]:
        artifact = self.store.get_artifact(artifact_id)
        path = self.store.artifact_path(artifact_id)
        if not path.is_file():
            raise KeyError(artifact_id)
        return artifact, path
