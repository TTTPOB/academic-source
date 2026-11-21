"""HTTP client for a running academic-source server.

Only httpx and the standard library may be imported here: this module is what
the default installation (without the ``server`` extra) runs.
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypedDict, Unpack

import httpx

_ACTIVE = ("queued", "running")
SearchBackend = Literal["openalex", "crossref", "pubmed"]


def open_client(server: str, api_key_file: str | Path | None = None) -> httpx.Client:
    key = os.environ.get("ACADEMIC_SOURCE_API_KEY")
    origin = "ACADEMIC_SOURCE_API_KEY"
    if key is None and api_key_file:
        # Key files conventionally end with a newline; the environment value is exact.
        key = Path(api_key_file).expanduser().read_text(encoding="utf-8").strip()
        origin = "The API key file"
    if key is not None and (
        not key or any(not 33 <= ord(character) <= 126 for character in key)
    ):
        raise ValueError(
            f"{origin} must be a non-empty ASCII string without whitespace"
        )
    return httpx.Client(
        base_url=server.rstrip("/") + "/",
        timeout=30.0,
        follow_redirects=True,
        headers={"Authorization": f"Bearer {key}"} if key else {},
    )


def _json(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


SearchFilters = TypedDict(
    "SearchFilters",
    {
        "backend": SearchBackend | None,
        "journal": str | None,
        "year": str | None,
        "type": str | None,
        "oa": bool,
        "min_citations": int | None,
        "author": str | None,
        "sort": str | None,
        "in": Literal["all", "title", "abstract"],
        "filter": str | None,
    },
    total=False,
)


def search(
    client: httpx.Client,
    query: str = "",
    limit: int = 10,
    **filters: Unpack[SearchFilters],
) -> Any:
    """Search one backend (OpenAlex by default); use **{"in": "title"} for scope."""
    params: dict[str, Any] = {"limit": limit}
    if query:
        params["query"] = query
    for key, value in filters.items():
        if key not in SearchFilters.__annotations__:
            raise TypeError(f"Unknown search option: {key}")
        if value is not None:
            params[key] = value
    response = client.get("api/v1/search", params=params)
    if response.status_code == 400:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            raise ValueError(body["detail"])
    return _json(response)


def metadata(client: httpx.Client, *, doi: str | None, arxiv: str | None) -> Any:
    body = (
        {"id_type": "doi", "doi": doi}
        if doi
        else {"id_type": "arxiv", "arxiv_id": arxiv}
    )
    return _json(client.post("api/v1/metadata", json=body))


def job(client: httpx.Client, job_id: str) -> dict[str, Any]:
    return _json(client.get(f"api/v1/jobs/{job_id}"))


def cancel(client: httpx.Client, job_id: str) -> dict[str, Any]:
    return _json(client.post(f"api/v1/jobs/{job_id}/cancel"))


def submit(
    client: httpx.Client,
    *,
    identifier: str | None = None,
    list_file: Path | None = None,
    policy: str,
    markdown: bool = False,
    supplementary: bool = False,
    supplementary_only: bool = False,
    bibtex: bool = False,
) -> dict[str, Any]:
    request: dict[str, Any]
    if list_file is not None:
        with list_file.open("rb") as stream:
            upload = _json(
                client.post("api/v1/uploads", files={"file": (list_file.name, stream)})
            )
        request = {"upload_id": upload["id"]}
    else:
        request = {"identifiers": [identifier]}
    request.update(
        policy=policy, markdown=markdown, supplementary=supplementary, bibtex=bibtex
    )
    if supplementary_only:
        request["supplementary_only"] = True
    return _json(client.post("api/v1/acquisitions", json=request))


def save_path(output: Path, item: dict[str, Any]) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    # An artifact's display filename never becomes an arbitrary output path.
    return output / f"{item['id']}-{Path(item['filename']).name}"


def download(client: httpx.Client, job: dict[str, Any], output: Path) -> list[Path]:
    """Save every artifact and record its local `path` in the job data."""
    saved = []
    per_paper = {
        item["id"]: item
        for result in job.get("results", [])
        for item in result.get("artifacts", [])
    }
    for item in job["artifacts"]:
        path = save_path(output, item)
        partial = path.with_suffix(path.suffix + ".part")
        try:
            with client.stream("GET", item["download_url"].lstrip("/")) as response:
                response.raise_for_status()
                with partial.open("wb") as destination:
                    for chunk in response.iter_bytes():
                        destination.write(chunk)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
        saved.append(path)
        item["path"] = str(path)
        if item["id"] in per_paper:
            per_paper[item["id"]]["path"] = str(path)
    return saved


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class Progress:
    """Print one stderr line per state change so a watcher sees the job move."""

    def __init__(self) -> None:
        self._state = ""
        self._reported: Counter[str] = Counter()

    def update(self, job: dict[str, Any]) -> None:
        # Papers finish out of order, so new results can appear anywhere in the list.
        seen: Counter[str] = Counter()
        for result in job.get("results", []):
            identifier = result["identifier"]
            seen[identifier] += 1
            if seen[identifier] <= self._reported[identifier]:
                continue
            self._reported[identifier] += 1
            done = sum(self._reported.values())
            parts = [f"[{done}/{job.get('total', '?')}]", identifier]
            parts.append(result["status"])
            if result.get("reason"):
                parts.append(result["reason"])
            if result.get("cached"):
                parts.append("cached")
            if result.get("elapsed_seconds") is not None:
                parts.append(f"{result['elapsed_seconds']:.1f}s")
            _say(" ".join(parts))
        state = job["status"]
        if state == "queued" and "queue" in job:
            state = f"queued: {job['queue']['ahead']} job(s) ahead"
        elif state == "running" and "progress" in job:
            progress = job["progress"]
            state = (
                f"running: paper {progress['current']}/{progress['total']} "
                f"{progress['identifier']}"
            )
        if state != self._state:
            self._state = state
            _say(f"job {job['id']} {state}")


def wait(
    client: httpx.Client,
    job: dict[str, Any],
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Poll until the job ends; ask the server to cancel it once `cancelled()` is true."""
    progress = Progress()
    progress.update(job)
    cancel_sent = False
    while job["status"] in _ACTIVE:
        if cancelled() and not cancel_sent:
            # Stopping this client must not leave the job running on the server.
            cancel_sent = True
            response = client.post(f"api/v1/jobs/{job['id']}/cancel")
            if response.status_code in (404, 405):
                _say(f"The server cannot cancel jobs; job {job['id']} keeps running.")
                return job
            response.raise_for_status()
            _say(f"Cancelling job {job['id']}; the paper in progress finishes first.")
        time.sleep(0.5)
        job = _json(client.get(f"api/v1/jobs/{job['id']}"))
        progress.update(job)
    return job
