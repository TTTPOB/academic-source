"""Real HTTP and local CLI behavior with static Bearer authentication."""

import json

import pytest

from academic_source.interfaces import cli
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource
from tests.academic_source.test_interfaces import running_server


def test_remote_cli_sends_bearer_for_upload_jobs_and_permanent_download(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    keys = ["offline-cli-first", "offline-cli-second"]
    source = RecordingSource()
    service = Application(
        Settings(data_dir=tmp_path / "server", api_keys=keys), source=source
    )
    listing = tmp_path / "references.csv"
    listing.write_text("doi,title\n10.1234/example,Offline paper\n", encoding="utf-8")
    output = tmp_path / "client-output"
    with running_server(service) as server:
        command = ["batch", str(listing), "--server", server, "--output", str(output)]
        monkeypatch.delenv("ACADEMIC_SOURCE_API_KEY", raising=False)
        assert cli.main(command) == 1
        assert source.calls == []
        monkeypatch.setenv("ACADEMIC_SOURCE_API_KEY", "incorrect-key")
        assert cli.main(command) == 1
        assert source.calls == []
        monkeypatch.setenv("ACADEMIC_SOURCE_API_KEY", keys[1])
        assert cli.main(command) == 0
    captured = capsys.readouterr()
    assert "401" in captured.err
    assert all(key not in captured.out + captured.err for key in keys)
    result = json.loads(captured.out)
    assert result["status"] == "succeeded"
    assert result["artifacts"][0]["download_url"].startswith("/api/v1/artifacts/")
    assert source.calls == ["10.1234/example"]
    exported = list(output.glob("*.pdf"))
    assert len(exported) == 1
    assert exported[0].read_bytes() == source.content


@pytest.mark.parametrize("key", ["private-cli-key\n", "private-cli-key\t", ""])
def test_remote_cli_rejects_invalid_key_without_printing_secret(
    tmp_path, monkeypatch, capsys, key
):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("ACADEMIC_SOURCE_API_KEY", key)
    source = RecordingSource()
    service = Application(
        Settings(data_dir=tmp_path / "server", api_keys=["offline-server-key"]),
        source=source,
    )
    with running_server(service) as server:
        assert cli.main(["get", "10.1234/example", "--server", server]) == 1
    captured = capsys.readouterr()
    assert "private-cli-key" not in captured.out + captured.err
    assert captured.out == ""
    assert captured.err.strip() == (
        "ACADEMIC_SOURCE_API_KEY must be a non-empty ASCII string without whitespace"
    )
    assert source.calls == []


def test_local_cli_runs_without_http_keys(tmp_path, monkeypatch, capsys):
    from academic_source.services import application

    monkeypatch.setattr(application, "LegacySources", RecordingSource)
    monkeypatch.setenv("ACADEMIC_SOURCE_DATA_DIR", str(tmp_path / "local"))
    monkeypatch.delenv("ACADEMIC_SOURCE_AUTH_MODE", raising=False)
    monkeypatch.delenv("ACADEMIC_SOURCE_API_KEYS", raising=False)
    monkeypatch.delenv("ACADEMIC_SOURCE_API_KEY", raising=False)
    output = tmp_path / "export"
    assert cli.main(["get", "10.1234/example", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert len(list(output.glob("*.pdf"))) == 1


def test_serve_without_keys_exits_before_creating_business_resources(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "not-created"
    monkeypatch.setenv("ACADEMIC_SOURCE_DATA_DIR", str(root))
    monkeypatch.delenv("ACADEMIC_SOURCE_AUTH_MODE", raising=False)
    monkeypatch.delenv("ACADEMIC_SOURCE_API_KEYS", raising=False)
    assert cli.main(["serve"]) == 1
    assert "requires at least one API key" in capsys.readouterr().err
    assert not root.exists()
