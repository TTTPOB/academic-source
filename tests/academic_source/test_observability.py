"""Timing, queue position, and per-attempt logs; old records stay readable."""

from __future__ import annotations

import logging
from threading import Event

import pymupdf
import pytest

from academic_source import sources
from academic_source.domain import AcquisitionRequest, Attempt, Job
from academic_source.interfaces.presentation import job_data
from academic_source.services.application import Application
from academic_source.settings import Settings
from academic_source.sources import oa
from academic_source.sources.models import SourceFailure
from tests.academic_source.helpers import RecordingSource, isolate_upstreams


@pytest.fixture(autouse=True)
def offline_upstreams(monkeypatch):
    isolate_upstreams(monkeypatch)


class Response:
    def __init__(self, url, *, body=b"", payload=None, status=200):
        self.url = url
        self.status_code = status
        self.body = body
        self.payload = payload or {}
        self.encoding = "utf-8"
        self.headers = {}

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


def test_job_records_start_time_and_per_paper_elapsed(tmp_path):
    application = Application(Settings(data_dir=tmp_path), source=RecordingSource())
    try:
        job = application.wait(
            application.submit(AcquisitionRequest(identifiers=["10.1234/a"])).id, 5
        )
    finally:
        application.close()
    assert job.started_at is not None and job.started_at >= job.created_at
    assert job.results[0].elapsed_seconds is not None
    assert job.current_identifier == ""
    data = job_data(job)
    assert data["started_at"] == job.started_at
    assert data["results"][0]["elapsed_seconds"] >= 0
    assert "queue" not in data and "progress" not in data


def test_queued_job_reports_jobs_ahead_and_running_job_reports_current_paper(
    tmp_path,
):
    started, release = Event(), Event()

    class BlockingSource(RecordingSource):
        def acquire(self, identifier, request, work_dir, config):
            if identifier.endswith("second"):
                started.set()
                assert release.wait(5)
            return super().acquire(identifier, request, work_dir, config)

    application = Application(Settings(data_dir=tmp_path), source=BlockingSource())
    try:
        first = application.submit(
            AcquisitionRequest(identifiers=["10.1234/first", "10.1234/second"])
        )
        assert started.wait(5)
        second = application.submit(AcquisitionRequest(identifiers=["10.1234/b"]))
        third = application.submit(AcquisitionRequest(identifiers=["10.1234/c"]))

        running = job_data(application.job(first.id))
        assert running["progress"] == {
            "current": 2,
            "total": 2,
            "identifier": "10.1234/second",
        }
        assert "queue" not in running
        assert job_data(second)["queue"] == {"ahead": 1}
        queued = job_data(application.job(third.id))
        assert queued["queue"] == {"ahead": 2}
        assert "started_at" not in queued and "progress" not in queued
        # Transient read-side fields never reach the stored record.
        assert application.store.get_job(third.id).queue_ahead is None

        release.set()
        assert application.wait(third.id, 5).status == "succeeded"
        assert "queue" not in job_data(application.job(third.id))
    finally:
        release.set()
        application.close()


def test_records_written_before_the_new_fields_still_load():
    job = Job.model_validate_json(
        '{"id":"j","status":"failed","request":{"identifiers":["10.1/x"]},'
        '"total":1,"completed":1,"error":"",'
        '"created_at":"2026-01-01T00:00:00+00:00",'
        '"updated_at":"2026-01-01T00:00:01+00:00",'
        '"results":[{"identifier":"10.1/x","status":"failed","reason":"not_found",'
        '"attempts":[{"source":"Unpaywall","status":"discovered"}]}]}'
    )
    assert job.started_at is None
    assert job.results[0].elapsed_seconds is None
    assert job.results[0].attempts[0].seconds is None
    assert job.results[0].attempts[0].candidates is None
    data = job_data(job)
    assert "started_at" not in data
    assert "elapsed_seconds" not in data["results"][0]


def test_oa_attempts_carry_duration_and_candidate_count_and_are_logged(
    monkeypatch, tmp_path, caplog
):
    secret = "https://repo.org/file?token=SECRET"

    def fetch(url, config, **kwargs):
        if "api.unpaywall.org" in url:
            return Response(url, payload={"best_oa_location": {"url_for_pdf": secret}})
        if url == secret:
            return Response(url, status=403)
        # Indexed by the service, but without any open location.
        return Response(url, payload={})

    monkeypatch.setattr(oa, "fetch", fetch)
    monkeypatch.setattr(oa, "polite_delay", lambda config: None)
    with caplog.at_level(logging.INFO, logger="academic_source.sources"):
        result = oa.acquire_oa("10.1234/paper", tmp_path, {"email": "a@uni.edu"})
    assert isinstance(result, SourceFailure)
    assert all(item.seconds is not None for item in result.attempts)
    discovered = {
        item.source: item.candidates
        for item in result.attempts
        if item.status == "discovered"
    }
    assert discovered == {"Unpaywall": 1, "OpenAlexOA": 0, "EuropePMC": 0, "PMC": 0}
    downloads = [item for item in result.attempts if item.status == "failed"]
    assert [item.candidates for item in downloads] == [None]
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == len(result.attempts)
    assert any(
        "identifier=10.1234/paper source=Unpaywall status=failed reason=blocked" in line
        for line in lines
    )
    assert "SECRET" not in "\n".join(lines)


def test_legacy_source_attempts_are_timed_and_logged(monkeypatch, tmp_path, caplog):
    def paper(doi, path, config):
        with pymupdf.open() as document:
            document.new_page()
            document.save(path)
        return {"success": True, "file": str(path)}

    monkeypatch.setattr(
        sources, "_plan", lambda *args: [("Miss", lambda *a: None), ("Hit", paper)]
    )
    with caplog.at_level(logging.INFO, logger="academic_source.sources"):
        result = sources.LegacySources().acquire(
            "10.1234/paper",
            AcquisitionRequest(identifiers=["10.1234/paper"], policy="grey_only"),
            tmp_path,
            {},
        )
    assert [(item.source, item.status) for item in result.attempts] == [
        ("Miss", "failed"),
        ("Hit", "succeeded"),
    ]
    assert all(
        isinstance(item, Attempt) and item.seconds >= 0 for item in result.attempts
    )
    assert [record.getMessage().split(" seconds=")[0] for record in caplog.records] == [
        "attempt identifier=10.1234/paper source=Miss status=failed reason=not_found candidates=-",
        "attempt identifier=10.1234/paper source=Hit status=succeeded reason=- candidates=-",
    ]
