"""The command line as a client of a running server, over real loopback HTTP."""

from __future__ import annotations

import json
import subprocess
import sys
from threading import Event

import pytest

from academic_source import client as remote
from academic_source.interfaces import cli
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource
from tests.academic_source.test_interfaces import running_server

KEY = "offline-client-key"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("ACADEMIC_SOURCE_API_KEY", KEY)
    monkeypatch.delenv("ACADEMIC_SOURCE_SERVER", raising=False)
    source = RecordingSource()
    service = Application(
        Settings(data_dir=tmp_path / "server", api_keys=[KEY]), source=source
    )
    with running_server(service) as url:
        yield url, source


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_default_installation_runs_client_commands_without_server_packages():
    blocked = [
        "pydantic",
        "fastapi",
        "uvicorn",
        "mcp",
        "pymupdf",
        "requests",
        "bs4",
        "openpyxl",
        "scansci_pdf",
    ]
    script = f"""
import importlib.abc, sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in {blocked!r}:
            raise ImportError(f"{{name}} is not installed")

sys.meta_path.insert(0, Block())
from academic_source.interfaces import cli

codes = [
    cli.main(["search", "x", "--server", "http://127.0.0.1:1"]),
    cli.main(["serve"]),
    cli.main(["get", "10.1234/x"]),
]
print(codes)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "", "NO_PROXY": "127.0.0.1"},
    )
    assert result.stdout.strip() == "[1, 1, 1]"
    lines = result.stderr.strip().splitlines()
    # A refused connection, not a missing module, ends the client command.
    assert "is not installed" not in lines[0]
    assert "academic-source[server]" in lines[1]
    assert "ACADEMIC_SOURCE_SERVER" in lines[2]


def test_batch_reports_progress_on_stderr_and_keeps_json_on_stdout(
    server, tmp_path, capsys
):
    url, _source = server
    listing = tmp_path / "refs.txt"
    listing.write_text("10.1234/first\n10.1234/missing\n", encoding="utf-8")
    code, out, err = run(
        capsys, "batch", str(listing), "--server", url, "--output", str(tmp_path / "o")
    )
    assert code == 1
    job = json.loads(out)
    assert job["status"] == "partial"
    lines = err.strip().splitlines()
    assert any(
        line.startswith("[1/2] 10.1234/first succeeded") and line.endswith("s")
        for line in lines
    )
    assert any(line.startswith("[2/2] 10.1234/missing failed") for line in lines)
    assert lines[-1] == f"job {job['id']} partial"
    assert len(list((tmp_path / "o").glob("*.pdf"))) == 1


def test_files_are_saved_only_when_asked(server, tmp_path, capsys):
    url, source = server
    output = tmp_path / "papers"
    code, out, _ = run(
        capsys,
        "get",
        "10.1234/example",
        "--server",
        url,
        "--output",
        str(output),
        "--no-download",
    )
    assert code == 0
    job_id = json.loads(out)["id"]
    assert not output.exists()

    code, out, _ = run(capsys, "status", job_id, "--server", url)
    assert code == 0 and json.loads(out)["status"] == "succeeded"
    assert not output.exists()

    code, _, _ = run(
        capsys, "download", job_id, "--server", url, "--output", str(output)
    )
    assert code == 0
    saved = list(output.glob("*.pdf"))
    assert len(saved) == 1 and saved[0].read_bytes() == source.content


def test_search_and_metadata_go_to_the_server(server, monkeypatch, capsys):
    url, _ = server
    monkeypatch.setattr(
        Application, "search", lambda self, query, limit: [{"q": query, "n": limit}]
    )
    monkeypatch.setattr(
        Application,
        "get_metadata",
        lambda self, id_type, doi=None, arxiv_id=None: {
            "id_type": id_type,
            "doi": doi,
            "arxiv_id": arxiv_id,
        },
    )
    monkeypatch.setenv("ACADEMIC_SOURCE_SERVER", url)
    code, out, _ = run(capsys, "search", "base editing", "--limit", "3")
    assert code == 0 and json.loads(out) == [{"q": "base editing", "n": 3}]
    code, out, _ = run(capsys, "metadata", "--arxiv", "2401.00001")
    assert code == 0 and json.loads(out)["arxiv_id"] == "2401.00001"
    code, out, _ = run(capsys, "metadata", "--doi", "10.1234/x")
    assert code == 0 and json.loads(out)["id_type"] == "doi"


def test_key_file_is_used_when_the_environment_has_no_key(
    server, tmp_path, monkeypatch, capsys
):
    url, _ = server
    monkeypatch.delenv("ACADEMIC_SOURCE_API_KEY")
    code, _, err = run(capsys, "status", "unknown", "--server", url)
    assert code == 1 and "401" in err
    key_file = tmp_path / "key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    code, _, err = run(
        capsys, "status", "unknown", "--server", url, "--api-key-file", str(key_file)
    )
    assert code == 1 and "404" in err and KEY not in err


@pytest.mark.parametrize("command", ["status", "download", "cancel"])
def test_job_commands_require_a_server(command, monkeypatch, capsys):
    monkeypatch.delenv("ACADEMIC_SOURCE_SERVER", raising=False)
    code, out, err = run(capsys, command, "some-job")
    assert code == 1 and out == ""
    assert "needs --server or ACADEMIC_SOURCE_SERVER" in err


def test_stopping_the_client_cancels_the_job_on_the_server(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("ACADEMIC_SOURCE_API_KEY", KEY)
    started, release = Event(), Event()

    class BlockingSource(RecordingSource):
        def acquire(self, identifier, request, work_dir, config):
            started.set()
            assert release.wait(10)
            return super().acquire(identifier, request, work_dir, config)

    source = BlockingSource()
    service = Application(
        Settings(data_dir=tmp_path / "server", api_keys=[KEY]), source=source
    )
    listing = tmp_path / "refs.txt"
    listing.write_text("10.1234/first\n10.1234/second\n", encoding="utf-8")
    with running_server(service) as url, remote.open_client(url) as client:
        job = remote.submit(client, list_file=listing, policy="fastest")
        assert started.wait(5)

        def stop_requested() -> bool:
            # The first paper is released only after the cancel request went out.
            if remote.job(client, job["id"])["status"] != "running":
                return True
            release.set()
            return True

        final = remote.wait(client, job, stop_requested)
        assert final["status"] == "cancelled"
        assert [item["identifier"] for item in final["results"]] == ["10.1234/first"]
        assert source.calls == ["10.1234/first"]
        assert "Cancelling job" in capsys.readouterr().err
        # Cancelling a finished job is a no-op that still reports it.
        code, out, _ = run(capsys, "cancel", job["id"], "--server", url)
        assert code == 0 and json.loads(out)["status"] == "cancelled"
