"""One application owns a data directory until it closes."""

import subprocess
import sys

import pytest

from academic_source.domain import AcquisitionRequest, Job
from academic_source.services.application import Application
from academic_source.settings import Settings


def test_application_lock_precedes_interruption_and_releases_on_close(tmp_path):
    settings = Settings(data_dir=tmp_path)
    first = Application(settings)
    first.store.save_job(
        Job(
            id="pending",
            status="running",
            request=AcquisitionRequest(identifiers=["10.1/test"]),
            created_at="2026-01-01",
            updated_at="2026-01-01",
        )
    )
    try:
        with pytest.raises(RuntimeError, match="--server"):
            Application(settings)
        assert first.job("pending").status == "running"
        script = (
            "import sys\nfrom pathlib import Path\n"
            "from academic_source.services.application import Application\n"
            "from academic_source.settings import Settings\n"
            "Application(Settings(data_dir=Path(sys.argv[1])))"
        )
        probe = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode != 0
        assert "use --server" in probe.stderr
        assert first.job("pending").status == "running"
    finally:
        first.close()
    second = Application(settings)
    try:
        assert second.job("pending").status == "interrupted"
    finally:
        second.close()
