"""Shared process-local admission and safe JSON/text requests.

Admission and short cooldowns are shared by service; budget observations belong to the
credential actually sent (or the anonymous egress), not an application Bearer.
No automatic retries or long budget-reset sleeps are performed here.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import requests

from .upstream_errors import RateLimited, UpstreamError, source_label

Operation = Literal["search", "list", "lookup", "content"]
Timeout = float | tuple[int, int]
_COSTS = {"search": 10, "list": 1, "lookup": 0, "content": 100}
_HOST_CONCURRENCY = {
    "unpaywall.org": 3,
    "api.openaire.eu": 3,
    "doaj.org": 3,
    "api.core.ac.uk": 3,
    "europepmc.org": 3,
    "www.ebi.ac.uk": 3,
    "arxiv.org": 2,
}


@dataclass
class _Gate:
    active: int = 0
    last_start: float = -math.inf
    cooldown: RateLimited | None = None


@dataclass
class _Observation:
    remaining: int | None = None
    reset_at: float | None = None


class _Admission:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.gates: dict[str, _Gate] = {}
        self.observations: dict[tuple[str, str, str], _Observation] = {}

    def acquire(
        self,
        source: str,
        identity: tuple[str, str, str],
        cost: int,
        interval: float,
        concurrency: int,
    ) -> None:
        with self.condition:
            gate = self.gates.setdefault(source, _Gate())
            while True:
                self._check_limits(source, identity, cost)
                delay = interval - (time.monotonic() - gate.last_start)
                if (concurrency <= 0 or gate.active < concurrency) and delay <= 0:
                    gate.active += 1
                    gate.last_start = time.monotonic()
                    return
                # Condition.wait releases the lock, so other services can proceed.
                self.condition.wait(max(delay, 0.001) if delay > 0 else None)

    def _check_limits(
        self, source: str, identity: tuple[str, str, str], cost: int
    ) -> None:
        now = time.time()
        cooldown = self.gates[source].cooldown
        if (
            cooldown is not None
            and cooldown.retry_at is not None
            and cooldown.retry_at > now
        ):
            raise RateLimited(
                source,
                kind=cooldown.kind,
                retry_at=cooldown.retry_at,
                remaining=cooldown.remaining,
                required=cost if source == "openalex" else None,
            )
        observation = self.observations.get(identity)
        if observation is None:
            return
        if (
            observation.reset_at is not None
            and observation.reset_at > now
            and observation.remaining is not None
            and observation.remaining < cost
        ):
            raise RateLimited(
                source,
                kind="daily_budget",
                retry_at=observation.reset_at,
                remaining=observation.remaining,
                required=cost,
            )

    def release(self, source: str) -> None:
        with self.condition:
            self.gates[source].active -= 1
            self.condition.notify_all()

    def observe(
        self,
        source: str,
        identity: tuple[str, str, str],
        cost: int,
        response: requests.Response,
    ) -> RateLimited | None:
        now = time.time()
        remaining = (
            _integer_header(response, "X-RateLimit-Remaining")
            if source == "openalex"
            else None
        )
        reset_seconds = (
            _seconds(response.headers.get("X-RateLimit-Reset"))
            if source == "openalex"
            else None
        )
        reset_at = now + reset_seconds if reset_seconds is not None else None
        error = None
        if response.status_code == 429:
            if remaining is not None and remaining < cost and reset_at is not None:
                error = RateLimited(
                    source,
                    kind="daily_budget",
                    retry_at=reset_at,
                    remaining=remaining,
                    required=cost,
                )
            else:
                kind = "request_rate" if _is_request_rate(response) else "unknown"
                retry_at = _retry_after(response, now)
                error = RateLimited(
                    source,
                    kind=kind,
                    retry_at=retry_at
                    if retry_at is not None
                    else now + (1 if kind == "request_rate" else 2),
                    remaining=remaining,
                    required=cost if source == "openalex" else None,
                )
        with self.condition:
            observation = self.observations.setdefault(identity, _Observation())
            if (
                (response.status_code < 400 or error is not None)
                and remaining is not None
                and reset_at is not None
            ):
                observation.remaining = remaining
                observation.reset_at = reset_at
            # Daily observations apply only to operations costing more than the
            # remaining budget. In particular a free lookup remains admissible.
            if error is not None and error.kind != "daily_budget":
                self.gates[source].cooldown = error
            self.condition.notify_all()
        return error


_admission = _Admission()


def _service(url: str, source: str | None) -> tuple[str, str]:
    host = (urlsplit(url).hostname or "").lower()
    if source is not None:
        return source, host
    if host in {"api.openalex.org", "content.openalex.org"}:
        return "openalex", host
    if host == "api.crossref.org":
        return "crossref", host
    if host == "export.arxiv.org":
        return "arxiv", host
    port = urlsplit(url).port
    return f"{host}:{port}" if port is not None else host, host


def _policy(source: str, host: str, config: dict[str, Any]) -> tuple[float, int]:
    # OpenAlex officially permits 100 RPS. Ten shared starts/second leaves
    # ample headroom without restoring the obsolete two-RPS search bottleneck.
    if source == "openalex":
        return 0.1, 5
    if source == "crossref":
        return 0.2, 1
    if source == "arxiv":
        return 3.0, 1
    custom = config.get("host_concurrency", {})
    if isinstance(custom, dict):
        if source in custom:
            return 0.5, int(custom[source])
        if host in custom:
            return 0.5, int(custom[host])
    for pattern, limit in _HOST_CONCURRENCY.items():
        if host == pattern or host.endswith("." + pattern):
            return 0.5, limit
    return 0.5, 0


def _operation(
    url: str, params: dict[str, Any] | None, operation: Operation | None
) -> Operation:
    if operation is not None:
        return operation
    parsed = urlsplit(url)
    if parsed.hostname == "content.openalex.org":
        return "content"
    query = {**parse_qs(parsed.query), **(params or {})}
    if "search" in query:
        return "search"
    return "lookup" if len(parsed.path.strip("/").split("/")) > 1 else "list"


def _identity(
    source: str,
    url: str,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    session: requests.Session,
    proxies: dict[str, str] | None,
) -> tuple[str, str, str]:
    query = parse_qs(urlsplit(url).query)
    credential = str(
        (params or {}).get("api_key") or next(iter(query.get("api_key", [])), "")
    )
    actual_headers = requests.structures.CaseInsensitiveDict(session.headers)
    actual_headers.update(headers or {})
    authorization = actual_headers.get("Authorization", "")
    if not credential and authorization.lower().startswith("bearer "):
        credential = authorization[7:]
    effective_proxies = {**session.proxies, **(proxies or {})}
    egress = str(effective_proxies.get(urlsplit(url).scheme, ""))
    # Credentials are private in-memory dictionary keys, never serialized.
    return source, credential, egress if not credential else ""


def _seconds(value: str | None) -> float | None:
    try:
        number = float(value) if value is not None else None
    except ValueError:
        return None
    return (
        number if number is not None and math.isfinite(number) and number >= 0 else None
    )


def _integer_header(response: requests.Response, name: str) -> int | None:
    try:
        value = int(response.headers[name])
    except (KeyError, ValueError):
        return None
    return value if value >= 0 else None


def _retry_after(response: requests.Response, now: float) -> float | None:
    value = response.headers.get("Retry-After")
    seconds = _seconds(value)
    if seconds is not None:
        return now + seconds
    if value is not None:
        try:
            return max(now, parsedate_to_datetime(value).timestamp())
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def _is_request_rate(response: requests.Response) -> bool:
    try:
        body = response.json()
    except (json.JSONDecodeError, requests.exceptions.JSONDecodeError):
        body = {}
    # Only recognize explicit rate signals in documented error/message fields.
    # Neither Retry-After nor generic "rate limit" proves an RPS violation.
    messages = (
        [body.get(field, "") for field in ("error", "message")]
        if isinstance(body, dict)
        else []
    )
    return any(
        marker in message.lower()
        for message in messages
        if isinstance(message, str)
        for marker in (
            "requests per second",
            "requests/second",
            "rps limit",
            "rate_limit_exceeded_per_second",
        )
    )


def request_response(
    url: str,
    config: dict[str, Any],
    *,
    session: requests.Session,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: Timeout | None = None,
    proxies: dict[str, str] | None = None,
    allow_redirects: bool = True,
    stream: bool = False,
    source: str | None = None,
    operation: Operation | None = None,
) -> requests.Response:
    """Admit one request; streamed responses hold their slot until close()."""
    from .network import request_timeout

    source, host = _service(url, source)
    cost = _COSTS[_operation(url, params, operation)] if source == "openalex" else 0
    identity = _identity(source, url, params, headers, session, proxies)
    interval, concurrency = _policy(source, host, config)
    _admission.acquire(source, identity, cost, interval, concurrency)
    response = None
    held_by_response = False
    try:
        response = session.request(
            method,
            url,
            params=params,
            headers=headers,
            timeout=timeout if timeout is not None else request_timeout(config),
            proxies=proxies,
            allow_redirects=allow_redirects,
            stream=stream,
        )
        error = _admission.observe(source, identity, cost, response)
        if error is not None:
            raise error
        if stream:
            original_close = response.close
            released = False

            def close() -> None:
                nonlocal released
                try:
                    original_close()
                finally:
                    if not released:
                        released = True
                        _admission.release(source)

            response.close = close
            held_by_response = True
        return response
    finally:
        if not held_by_response:
            try:
                if response is not None and response.status_code == 429:
                    response.close()
            finally:
                _admission.release(source)


def _read(
    url: str,
    config: dict[str, Any] | None,
    *,
    as_json: bool,
    source: str | None,
    operation: Operation | None,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    session: requests.Session | None,
    timeout: Timeout | None,
    proxies: dict[str, str] | None,
) -> Any:
    from .network import _get_session

    config = config if config is not None else {}
    source, _ = _service(url, source)
    label = source_label(source)
    try:
        response = request_response(
            url,
            config,
            session=session if session is not None else _get_session(config),
            source=source,
            operation=operation,
            params=params,
            headers=headers,
            timeout=timeout,
            proxies=proxies,
        )
    except requests.RequestException:
        raise UpstreamError(
            source, f"{label}: request failed (network error)"
        ) from None
    try:
        if response.status_code >= 400:
            raise UpstreamError(
                source,
                f"{label}: request failed (HTTP {response.status_code})",
                status_code=response.status_code,
            )
        if not as_json:
            return response.text
        try:
            return response.json()
        except (json.JSONDecodeError, requests.exceptions.JSONDecodeError):
            raise UpstreamError(source, f"{label}: returned invalid JSON") from None
    finally:
        response.close()


def get_json(
    url: str,
    config: dict[str, Any] | None = None,
    *,
    source: str | None = None,
    operation: Operation | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
    timeout: Timeout | None = None,
    proxies: dict[str, str] | None = None,
) -> Any:
    """Read and close one JSON response, leaving the caller's session alive."""
    return _read(
        url,
        config,
        as_json=True,
        source=source,
        operation=operation,
        params=params,
        headers=headers,
        session=session,
        timeout=timeout,
        proxies=proxies,
    )


def get_text(
    url: str,
    config: dict[str, Any] | None = None,
    *,
    source: str | None = None,
    operation: Operation | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
    timeout: Timeout | None = None,
    proxies: dict[str, str] | None = None,
) -> str:
    """Read and close one text response, leaving XML interpretation to callers."""
    return _read(
        url,
        config,
        as_json=False,
        source=source,
        operation=operation,
        params=params,
        headers=headers,
        session=session,
        timeout=timeout,
        proxies=proxies,
    )


def openalex_params(
    config: dict[str, Any], params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Add the configured upstream API key without changing caller parameters."""
    result = dict(params or {})
    if config.get("openalex_api_key") and "api_key" not in result:
        result["api_key"] = config["openalex_api_key"]
    return result


def openalex_content_url(work_id: str, config: dict[str, Any]) -> str:
    """Build a Content endpoint using the same upstream API-key identity."""
    url = f"https://content.openalex.org/works/{quote(work_id.rstrip('/').rsplit('/', 1)[-1], safe='')}.pdf"
    params = openalex_params(config)
    return f"{url}?{urlencode(params)}" if params else url
