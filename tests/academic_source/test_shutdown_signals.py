"""Process-level CLI signal regressions."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signals")
def test_sigterm_does_not_count_as_first_sigint() -> None:
    script = """\
import signal
import sys
import time
from academic_source.interfaces.signals import graceful_signals

with graceful_signals(lambda: print('stopping', file=sys.stderr, flush=True)):
    print('ready', flush=True)
    while True:
        time.sleep(0.1)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        assert process.stderr is not None
        process.send_signal(signal.SIGTERM)
        assert process.stderr.readline().strip() == "stopping"
        process.send_signal(signal.SIGINT)
        assert "press Ctrl+C again" in process.stderr.readline()
        # A SIGTERM followed by SIGINT must not restart the drain.
        assert process.poll() is None
        # The second Ctrl+C must bypass all Python thread cleanup.
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130
        assert stdout == ""
        assert stderr == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def _read_stderr_until(process: subprocess.Popen, needle: bytes) -> bytes:
    assert process.stderr is not None
    output = b""
    deadline = time.monotonic() + 5
    while needle not in output:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"Missing {needle!r}: {output!r}"
        ready, _, _ = select.select([process.stderr], [], [], remaining)
        assert ready, f"Missing {needle!r}: {output!r}"
        chunk = os.read(process.stderr.fileno(), 4096)
        assert chunk, f"Unexpected stderr EOF: {output!r}"
        output += chunk
    return output


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
@pytest.mark.parametrize("first_signal", [signal.SIGINT, signal.SIGTERM])
def test_stdio_first_interrupt_exits_with_stdin_open(
    tmp_path: Path, first_signal: signal.Signals
) -> None:
    started = tmp_path / "started"
    release = tmp_path / "release"
    process = subprocess.Popen(
        [sys.executable, "-c", FAKE_SOURCE, "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "ACADEMIC_SOURCE_DATA_DIR": str(tmp_path),
            "STARTED": str(started),
            "RELEASE": str(release),
        },
    )
    try:
        assert process.stdin is not None
        process.stdin.write(
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}\n'
        )
        process.stdin.flush()
        assert process.stdout is not None
        assert b'"jsonrpc":"2.0"' in process.stdout.readline()
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.write(
            b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"acquire","arguments":{"request":{"identifiers":["10.test/stdio"]},"wait_seconds":0}}}\n'
        )
        process.stdin.flush()
        reply = json.loads(process.stdout.readline())
        assert reply["id"] == 2
        _wait_file(started)
        process.send_signal(first_signal)
        if first_signal == signal.SIGTERM:
            time.sleep(0.1)
            assert process.poll() is None
            process.send_signal(signal.SIGINT)
        assert b"press Ctrl+C again" in _read_stderr_until(
            process, b"press Ctrl+C again"
        )
        assert process.poll() is None
        release.touch()
        assert process.wait(timeout=8) == 0  # stdin is still open here.
        stdout, stderr = process.communicate(timeout=5)
        assert stdout == b""
        assert stderr == b""
        from academic_source.infrastructure.storage import Store
        from academic_source.settings import Settings

        assert (
            Store(Settings.load(tmp_path))
            .get_job(json.loads(reply["result"]["content"][0]["text"])["id"])
            .status
            == "failed"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
def test_uvicorn_first_interrupt_is_not_replayed(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    started = tmp_path / "started"
    release = tmp_path / "release"
    process = subprocess.Popen(
        [sys.executable, "-c", FAKE_SOURCE, "serve", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "ACADEMIC_SOURCE_DATA_DIR": str(tmp_path),
            "STARTED": str(started),
            "RELEASE": str(release),
        },
    )
    try:
        url = f"http://127.0.0.1:{port}/api/v1/jobs/unknown"
        for _ in range(100):
            try:
                httpx.get(url, timeout=0.15)
                break
            except httpx.ConnectError:
                time.sleep(0.05)
        else:
            pytest.fail("Uvicorn failed to start")
        base = f"http://127.0.0.1:{port}"
        jobs = [
            httpx.post(
                base + "/api/v1/acquisitions",
                json={"identifiers": [f"10.test/{number}"]},
                timeout=3,
            ).json()["id"]
            for number in range(2)
        ]
        _wait_file(started)
        process.send_signal(signal.SIGINT)
        assert b"press Ctrl+C again" in _read_stderr_until(
            process, b"press Ctrl+C again"
        )
        assert process.poll() is None
        release.touch()
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0
        from academic_source.infrastructure.storage import Store
        from academic_source.settings import Settings

        store = Store(Settings.load(tmp_path))
        assert [store.get_job(job).status for job in jobs] == ["failed", "failed"]
        assert b"press Ctrl+C again" not in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


# Replace only the process's source boundary; serve, MCP and CLI stay real.
FAKE_SOURCE = """\
import os
import time
from pathlib import Path
from academic_source.sources.models import SourceFailure
import academic_source.services.application as module
class Source:
    def prepare(self, config): pass
    def close(self): pass
    def supports(self, identifier, request): return True
    def acquire(self, identifier, request, work_dir, config):
        Path(os.environ['STARTED']).write_text(identifier)
        while not Path(os.environ['RELEASE']).exists(): time.sleep(0.02)
        return SourceFailure(reason='not_found')
module.LegacySources = Source
from academic_source.interfaces.cli import main
raise SystemExit(main(__import__('sys').argv[1:]))
"""


def _wait_file(path: Path) -> None:
    for _ in range(150):
        if path.exists():
            return
        time.sleep(0.03)
    pytest.fail(f"Worker did not start: {path}")


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
def test_second_interrupt_escapes_blocked_local_worker(tmp_path: Path) -> None:
    started = tmp_path / "started"
    release = tmp_path / "release"
    process = subprocess.Popen(
        [sys.executable, "-c", FAKE_SOURCE, "get", "10.test/block"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "ACADEMIC_SOURCE_DATA_DIR": str(tmp_path),
            "STARTED": str(started),
            "RELEASE": str(release),
        },
    )
    try:
        _wait_file(started)
        process.send_signal(signal.SIGINT)
        assert process.stderr is not None
        assert b"press Ctrl+C again" in process.stderr.readline()
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=3)
        assert process.returncode == 130
        from academic_source.services.application import Application
        from academic_source.settings import Settings

        app = Application(Settings.load(tmp_path))
        try:
            import sqlite3

            with sqlite3.connect(tmp_path / "catalog.sqlite") as db:
                job_ids = [row[0] for row in db.execute("SELECT id FROM jobs")]
            assert job_ids
            assert app.job(job_ids[0]).status == "interrupted"
        finally:
            app.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
