"""Skip OA-only sources once Unpaywall and OpenAlex both deny open access."""

from __future__ import annotations

import pytest
import requests

from academic_source import sources
from academic_source.domain import AcquisitionRequest
from academic_source.sources import oa
from academic_source.sources.models import SourceFailure
from tests.academic_source.helpers import isolate_upstreams

OA_ONLY = {"OpenAIRE", "DOAJ", "CrossrefPage", "CORE"}


@pytest.fixture(autouse=True)
def offline_upstreams(monkeypatch):
    isolate_upstreams(monkeypatch)
    monkeypatch.setattr(oa, "polite_delay", lambda config: None)


class Response(requests.Response):
    def __init__(self, url, payload=None, status=200):
        super().__init__()
        self._content = b""
        self._content_consumed = True
        self.url = url
        self.status_code = status
        self.payload = payload or {}
        self.encoding = "utf-8"
        self.headers = {}

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield b"<html></html>"

    def close(self):
        pass


def run_fast_path(monkeypatch, tmp_path, unpaywall, openalex, pmcid=None):
    def fetch(url, config, **kwargs):
        if "api.unpaywall.org" in url:
            return Response(url, unpaywall)
        if "api.openalex.org" in url:
            return Response(url, openalex)
        if "pmc.ncbi.nlm.nih.gov/tools/idconv/" in url:
            records = [{"doi": "10.1126/science.x", "pmcid": pmcid}] if pmcid else []
            return Response(url, {"records": records})
        if "pmc-oa-opendata.s3.amazonaws.com" in url:
            return Response(url, status=404)
        return Response(url)

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa("10.1126/science.x", tmp_path, {"email": "a@uni.edu"})
    assert isinstance(result, SourceFailure)
    return result


def test_both_services_denying_open_access_marks_the_work_closed(monkeypatch, tmp_path):
    result = run_fast_path(
        monkeypatch, tmp_path, {"is_oa": False}, {"open_access": {"is_oa": False}}
    )
    assert result.closed_access is True
    verdicts = {item.source: item.open_access for item in result.attempts}
    assert verdicts == {
        "Unpaywall": False,
        "OpenAlexOA": False,
        "EuropePMC": None,
        "PMC": None,
    }


@pytest.mark.parametrize(
    ("unpaywall", "openalex", "pmcid"),
    [
        # A single verdict is not enough.
        ({"is_oa": False}, {}, None),
        ({}, {"open_access": {"is_oa": False}}, None),
        ({"is_oa": False}, {"open_access": {"is_oa": True}}, None),
        # A failed lookup is not a verdict.
        ({"is_oa": False}, None, None),
        # Any located copy outweighs both verdicts, even if it fails to download.
        (
            {"is_oa": False},
            {"open_access": {"is_oa": False}},
            "PMC1",
        ),
    ],
)
def test_work_is_not_closed_without_two_denials_and_zero_locations(
    monkeypatch, tmp_path, unpaywall, openalex, pmcid
):
    result = run_fast_path(monkeypatch, tmp_path, unpaywall, openalex, pmcid)
    assert result.closed_access is False


@pytest.mark.parametrize("policy", ["oa_first", "fastest", "legal_only"])
def test_plan_drops_only_oa_sources_when_asked(policy):
    request = AcquisitionRequest(identifiers=["10.1126/science.x"], policy=policy)
    config = {"scihub_enabled": True, "openalex_api_key": "key"}
    full = [label for label, _ in sources._plan("10.1126/science.x", request, config)]
    reduced = [
        label for label, _ in sources._plan("10.1126/science.x", request, config, True)
    ]
    assert OA_ONLY <= set(full)
    assert reduced == [label for label in full if label not in OA_ONLY]
    assert reduced


@pytest.mark.parametrize("closed", [True, False])
def test_acquire_passes_the_fast_path_verdict_to_the_plan(
    monkeypatch, tmp_path, closed
):
    seen = []
    monkeypatch.setattr(
        sources,
        "acquire_oa",
        lambda *args: SourceFailure(reason="not_found", closed_access=closed),
    )
    monkeypatch.setattr(sources, "_plan", lambda *args: seen.append(args[3:]) or [])
    sources.LegacySources().acquire(
        "10.1126/science.x",
        AcquisitionRequest(identifiers=["10.1126/science.x"], policy="oa_first"),
        tmp_path,
        {},
    )
    assert seen == [(closed,)]
