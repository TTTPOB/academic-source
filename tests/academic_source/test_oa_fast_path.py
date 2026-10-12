"""OA locations are discovery inputs, not separate competing download engines."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pymupdf
import pytest
import requests

from academic_source import sources
from academic_source.domain import AcquisitionRequest, Attempt
from academic_source.sources import oa
from academic_source.sources.models import SourceFailure, SourceSuccess
from academic_source.sources.oa_candidates import (
    Candidate,
)


class Response:
    def __init__(self, url, *, body=b"", payload=None, status=200, mime="text/html"):
        self.url = url
        self.status_code = status
        self.body = body
        self.payload = payload or {}
        self.encoding = "utf-8"
        self.headers = {"content-type": mime}
        self.closed = False

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


def pdf_bytes():
    with pymupdf.open() as document:
        document.new_page()
        return document.tobytes()


def test_pmc_without_pdf_field_downloads_immediately_and_stops_metadata(
    monkeypatch, tmp_path
):
    visited = []
    responses = []

    def fetch(url, config, **kwargs):
        visited.append(url)
        if "api.unpaywall.org" in url:
            response = Response(
                url,
                payload={
                    "best_oa_location": {
                        "url_for_pdf": None,
                        "url_for_landing_page": "https://www.ncbi.nlm.nih.gov/pmc/articles/134036",
                        "host_type": "repository",
                    }
                },
            )
        elif url == "https://europepmc.org/api/getPdf?pmcid=PMC134036":
            response = Response(url, body=pdf_bytes(), mime="application/pdf")
        else:
            pytest.fail(f"Unnecessary query/download: {url}")
        responses.append(response)
        return response

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa(
        "10.1128/MCB.22.19.6759-6766.2002", tmp_path, {"email": "reader@university.edu"}
    )
    assert isinstance(result, SourceSuccess)
    assert result.source == "EuropePMC"
    assert len(visited) == 2
    assert all(response.closed for response in responses)


def test_core_query_finds_pmc_before_render_and_id_conversion(monkeypatch, tmp_path):
    visited = []

    def fetch(url, config, **kwargs):
        visited.append(url)
        if "webservices/rest/search" in url:
            assert parse_qs(urlsplit(url).query)["resultType"] == ["core"]
            return Response(
                url,
                payload={
                    "resultList": {
                        "result": [
                            {
                                "pmcid": "PMC134036",
                                "isOpenAccess": "N",
                                "fullTextUrlList": {
                                    "fullTextUrl": [
                                        {
                                            "url": "https://europepmc.org/articles/PMC134036?pdf=render",
                                            "documentStyle": "pdf",
                                            "availability": "FreePDF",
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                },
            )
        if "api/getPdf" in url:
            return Response(url, body=pdf_bytes())
        assert "pdf=render" not in url
        assert "idconv" not in url
        return Response(url)

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa("10.1128/MCB.22.19.6759-6766.2002", tmp_path, {})
    assert isinstance(result, SourceSuccess)
    assert visited[-1] == "https://europepmc.org/api/getPdf?pmcid=PMC134036"


def test_duplicate_candidates_and_html_links_download_once_then_early_stop(
    monkeypatch, tmp_path
):
    visited = []
    landing = "https://repo.org/landing"
    api = "https://repo.org/api/file?id=42"

    def discover(doi, config):
        yield (
            [Candidate(landing, "First"), Candidate(landing + "#section", "First")],
            Attempt(source="First", status="discovered"),
        )
        yield (
            [Candidate(api, "Second"), Candidate("https://repo.org/good", "Second")],
            Attempt(source="Second", status="discovered"),
        )
        pytest.fail("Metadata queried after a usable file")

    def fetch(url, config, **kwargs):
        visited.append(url)
        if url == landing:
            return Response(
                url, body=f'<meta name="citation_pdf_url" content="{api}">'.encode()
            )
        if url == api:
            return Response(url, body=b"<html>Not a PDF</html>", mime="application/pdf")
        return Response(url, body=pdf_bytes())

    monkeypatch.setattr(oa, "discover", discover)
    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa("10.1234/paper", tmp_path, {})
    assert isinstance(result, SourceSuccess)
    assert visited == [landing, api, "https://repo.org/good"]
    assert any(item.reason == "no_pdf_found" for item in result.attempts)


def test_html_challenge_waits_for_other_http_before_bounded_cdp(monkeypatch, tmp_path):
    from scansci_pdf import browser_engine

    events = []
    urls = [f"https://repo.org/{number}" for number in range(3)]
    monkeypatch.setattr(
        oa,
        "discover",
        lambda *args: iter(
            [
                (
                    [Candidate(url, "OA") for url in urls],
                    Attempt(source="OA", status="discovered"),
                )
            ]
        ),
    )
    monkeypatch.setattr(
        oa,
        "fetch",
        lambda url, *args, **kwargs: (
            events.append(url) or Response(url, body=b"<html>reCAPTCHA</html>")
        ),
    )
    monkeypatch.setattr(
        browser_engine,
        "create_tab",
        lambda url, config, **kwargs: events.append("create") or "owned",
    )
    monkeypatch.setattr(browser_engine, "navigate_tab", lambda *args, **kwargs: True)
    monkeypatch.setattr(browser_engine, "get_snapshot", lambda *args: {})
    monkeypatch.setattr(
        browser_engine,
        "fetch_pdf_in_tab",
        lambda tab, url, path, config: events.append("browser:" + url) or False,
    )
    monkeypatch.setattr(
        browser_engine, "close_tab", lambda tab, config: events.append("close:" + tab)
    )
    result = oa.acquire_oa("10.1234/paper", tmp_path, {"browser_backend": "cdp"})
    assert isinstance(result, SourceFailure)
    assert events[:3] == urls
    assert events.count("create") == 2
    assert events.count("close:owned") == 2
    assert all(
        item.reason == "blocked" for item in result.attempts if item.status == "failed"
    )


def test_http_success_never_opens_cdp(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oa,
        "discover",
        lambda *args: iter(
            [
                (
                    [Candidate("https://repo.org/file", "OA")],
                    Attempt(source="OA", status="discovered"),
                )
            ]
        ),
    )
    monkeypatch.setattr(
        oa, "fetch", lambda url, *args, **kwargs: Response(url, body=pdf_bytes())
    )
    monkeypatch.setattr(
        oa, "_browser_candidate", lambda *args: pytest.fail("CDP opened")
    )
    assert isinstance(
        oa.acquire_oa("10.1234/paper", tmp_path, {"browser_backend": "cdp"}),
        SourceSuccess,
    )


@pytest.mark.parametrize(
    "status,reason", [(429, "rate_limited"), (503, "network_error"), (404, "not_found")]
)
def test_metadata_failure_is_not_missing(monkeypatch, status, reason):
    monkeypatch.setattr(
        oa, "fetch", lambda url, *args, **kwargs: Response(url, status=status)
    )
    payload, attempt = oa._metadata("OA", "https://repo.org/api", {})
    assert payload == {}
    assert attempt.reason == reason


def test_transport_error_is_not_missing(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout("timeout")

    monkeypatch.setattr(oa, "fetch", fail)
    assert oa._metadata("OA", "https://repo.org/api", {})[1].reason == "network_error"


@pytest.mark.parametrize("policy", ["fastest", "oa_first", "legal_only"])
def test_fast_path_success_precedes_publishers_and_stops(monkeypatch, tmp_path, policy):
    monkeypatch.setattr(
        oa,
        "discover",
        lambda *args: iter(
            [
                (
                    [Candidate("https://repo.org/file", "OA")],
                    Attempt(source="OA", status="discovered"),
                )
            ]
        ),
    )
    monkeypatch.setattr(
        oa, "fetch", lambda url, *args, **kwargs: Response(url, body=pdf_bytes())
    )
    monkeypatch.setattr(
        sources,
        "_plan",
        lambda *args: pytest.fail("Publisher plan reached after OA success"),
    )
    result = sources.LegacySources().acquire(
        "10.1234/paper",
        AcquisitionRequest(identifiers=["10.1234/paper"], policy=policy),
        tmp_path,
        {},
    )
    assert isinstance(result, SourceSuccess)


def test_fallback_reuses_download_dedupe_and_preserves_failure_action(
    monkeypatch, tmp_path
):
    from scansci_pdf import pdf_utils

    url = "https://repo.org/file.pdf"
    monkeypatch.setattr(
        oa,
        "discover",
        lambda *args: iter(
            [([Candidate(url, "OA")], Attempt(source="OA", status="discovered"))]
        ),
    )
    monkeypatch.setattr(
        oa, "fetch", lambda url, *args, **kwargs: Response(url, status=403)
    )
    monkeypatch.setattr(
        pdf_utils,
        "fetch",
        lambda *args, **kwargs: pytest.fail("Duplicate fallback download"),
    )

    def fallback(doi, path, config):
        return pdf_utils.download_pdf(url + "#view=fit", path, config, "Fallback")

    monkeypatch.setattr(sources, "_plan", lambda *args: [("Fallback", fallback)])
    result = sources.LegacySources().acquire(
        "10.1234/paper",
        AcquisitionRequest(identifiers=["10.1234/paper"], policy="legal_only"),
        tmp_path,
        {},
    )
    assert isinstance(result, SourceFailure)
    assert result.reason == "network_error"
    assert result.action == "retry_later"
    assert "blocked" in result.message
    assert result.attempts[-1].reason == "not_found"


def test_planner_does_not_repeat_consumed_metadata_or_publisher_aliases():
    handlers = sources._plan(
        "10.1016/example",
        AcquisitionRequest(identifiers=["10.1016/example"]),
        {"openalex_api_key": "test"},
    )
    assert {name for name, _ in handlers}.isdisjoint(oa.REPLACED_SOURCES)
    assert {"Crossref", "ElsevierBrowser", "OpenAIRE", "CORE"} <= {
        name for name, _ in handlers
    }


def test_dedupe_scope_ends_with_each_acquisition(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        oa,
        "discover",
        lambda *args: iter(
            [
                (
                    [Candidate("https://repo.org/file", "OA")],
                    Attempt(source="OA", status="discovered"),
                )
            ]
        ),
    )
    monkeypatch.setattr(
        oa,
        "fetch",
        lambda url, *args, **kwargs: (
            calls.append(url) or Response(url, body=pdf_bytes())
        ),
    )
    config = {}
    request = AcquisitionRequest(identifiers=["10.1234/paper"])
    for _ in range(2):
        assert isinstance(
            sources.LegacySources().acquire("10.1234/paper", request, tmp_path, config),
            SourceSuccess,
        )
    assert len(calls) == 2
    assert "_acquisition_attempted_urls" not in config


def test_cdp_can_retry_same_http_url_and_keeps_query_and_owned_tab(
    monkeypatch, tmp_path
):
    from scansci_pdf import browser_engine

    url = "https://repo.org/api/getPdf?pmcid=PMC134036&token=keep-this"
    events = []
    monkeypatch.setattr(
        oa,
        "discover",
        lambda *args: iter(
            [([Candidate(url, "OA")], Attempt(source="OA", status="discovered"))]
        ),
    )
    monkeypatch.setattr(oa, "fetch", lambda *args, **kwargs: Response(url, status=403))
    monkeypatch.setattr(browser_engine, "create_tab", lambda *args, **kwargs: "owned")
    monkeypatch.setattr(browser_engine, "navigate_tab", lambda *args, **kwargs: True)
    monkeypatch.setattr(browser_engine, "get_snapshot", lambda *args: {})

    def fetch_pdf(tab, target, path, config):
        assert target == url
        assert tab == "owned"
        assert config["browser_pdf_timeout"] == 20
        path.write_bytes(pdf_bytes())
        return True

    monkeypatch.setattr(browser_engine, "fetch_pdf_in_tab", fetch_pdf)
    monkeypatch.setattr(
        browser_engine, "close_tab", lambda tab, config: events.append(tab)
    )
    monkeypatch.setattr(
        browser_engine,
        "close_shared_browser",
        lambda *args: pytest.fail("Browser was closed"),
    )
    result = oa.acquire_oa("10.1234/paper", tmp_path, {"browser_backend": "cdp"})
    assert isinstance(result, SourceSuccess)
    assert result.url == url
    assert events == ["owned"]


def test_truncated_pdf_is_not_a_success(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oa,
        "discover",
        lambda *args: iter(
            [
                (
                    [Candidate("https://repo.org/file", "OA")],
                    Attempt(source="OA", status="discovered"),
                )
            ]
        ),
    )
    monkeypatch.setattr(
        oa,
        "fetch",
        lambda url, *args, **kwargs: Response(url, body=b"%PDF-1.7\ntruncated"),
    )
    result = oa.acquire_oa("10.1234/paper", tmp_path, {})
    assert isinstance(result, SourceFailure)
    assert result.attempts[-1].reason == "invalid_pdf"
    assert not list(tmp_path.iterdir())


def test_openalex_content_reuses_metadata_without_another_query(monkeypatch, tmp_path):
    visited = []

    def fetch(url, config, **kwargs):
        visited.append(url)
        if "api.openalex.org" in url:
            return Response(
                url,
                payload={
                    "id": "https://openalex.org/W42",
                    "open_access": {"is_oa": True},
                },
            )
        assert url == "https://content.openalex.org/works/W42.pdf?mailto=key"
        return Response(url, body=pdf_bytes())

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa("10.1234/paper", tmp_path, {"openalex_api_key": "key"})
    assert isinstance(result, SourceSuccess)
    assert result.source == "OpenAlexContent"
    assert len(visited) == 2
