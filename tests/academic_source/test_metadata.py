"""Observe on-demand Crossref metadata separately from OpenAlex discovery."""

import json
from urllib.parse import unquote, urlsplit

import pymupdf
import pytest
import requests
from fastapi.testclient import TestClient

from academic_source.app import create_app
from academic_source.domain import AcquisitionRequest
from academic_source.services import discovery
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource, paper_bytes


def response(request, message, status=200):
    result = requests.Response()
    result.status_code = status
    result.request = request
    result.url = request.url
    result._content = json.dumps(message).encode()
    result._content_consumed = True
    result.encoding = "utf-8"
    return result


@pytest.fixture
def transport(monkeypatch):
    def install(handler):
        def send(_session, request, **_kwargs):
            return handler(request)

        monkeypatch.setattr(requests.Session, "send", send)

    return install


def mcp_metadata(client, doi):
    return client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_metadata", "arguments": {"doi": doi}},
        },
    ).json()["result"]


def test_explicit_doi_metadata_preserves_identity_and_directed_relations(transport):
    doi = "10.1234/review1"

    def upstream(request):
        url = urlsplit(request.url)
        assert url.hostname == "api.crossref.org"
        assert unquote(url.path) == f"/works/{doi}"
        return response(
            request,
            {
                "message": {
                    "DOI": doi.upper(),
                    "title": ["<i>Review</i> &amp; decision"],
                    "author": [
                        {"given": "Ada", "family": "Lovelace"},
                        {"name": "Research Group"},
                    ],
                    "published-print": {"date-parts": [[]]},
                    "published-online": {"date-parts": [[2025, 2, 3]]},
                    "type": "peer-review",
                    "review": {"type": "editor-report", "stage": "pre-publication"},
                    "relation": {
                        "is-review-of": [
                            {"id-type": "doi", "id": "https://doi.org/10.1234/parent"}
                        ]
                    },
                    "update-to": [
                        {
                            "DOI": "10.1234/old",
                            "type": "correction",
                            "updated": {"date-parts": [[2024]]},
                        }
                    ],
                    "updated-by": [{"DOI": "10.1234/notice", "type": "retraction"}],
                    "deposited": {"private-extra": "not part of the public projection"},
                }
            },
        )

    transport(upstream)
    result = discovery.get_metadata(f"doi: {doi}", {})
    assert result == {
        "doi": doi,
        "metadata_status": "found",
        "metadata_source": "crossref",
        "title": "Review & decision",
        "authors": ["Ada Lovelace", "Research Group"],
        "year": 2025,
        "type": "peer-review",
        "relations": [{"type": "is-review-of", "doi": "10.1234/parent"}],
        "review_type": "editor-report",
        "updates": [
            {"direction": "update-to", "type": "correction", "doi": "10.1234/old"},
            {"direction": "updated-by", "type": "retraction", "doi": "10.1234/notice"},
        ],
    }


def test_doi_record_without_dates_or_optional_metadata_does_not_emit_empty_fields(
    transport,
):
    transport(
        lambda request: response(request, {"message": {"DOI": "10.1234/minimal"}})
    )
    assert discovery.get_metadata("10.1234/minimal", {}) == {
        "doi": "10.1234/minimal",
        "metadata_status": "found",
        "metadata_source": "crossref",
    }


@pytest.mark.parametrize(
    "failure", ["404", "429", "network", "json", "mismatched_doi", "invalid_record"]
)
def test_metadata_failure_is_explicit_without_discarding_normalized_doi(
    failure, transport
):
    def upstream(request):
        if failure == "network":
            raise requests.Timeout("could not reach provider")
        if failure in {"404", "429"}:
            return response(request, {}, status=int(failure))
        if failure == "json":
            result = response(request, {})
            result._content = b"invalid JSON"
            return result
        return response(
            request,
            {
                "message": {
                    "DOI": []
                    if failure == "invalid_record"
                    else "10.1234/some-other-work"
                }
            },
        )

    transport(upstream)
    result = discovery.get_metadata("https://doi.org/10.1234/known", {})
    assert result["doi"] == "10.1234/known"
    assert result["metadata_source"] == "crossref"
    assert result["metadata_status"] == (
        "not_found" if failure == "404" else "unavailable"
    )
    if failure != "404":
        assert "Crossref" in result["metadata_error"]
    assert "title" not in result


def test_acquire_title_and_direct_identifiers_do_not_require_crossref(
    tmp_path, transport
):
    title = "The role of ARID3C in cancer biology"
    # The legacy arXiv downloader also checks a minimum PDF size.
    with pymupdf.open(stream=paper_bytes(), filetype="pdf") as document:
        document.new_page().insert_text((72, 72), "Full text continues on page two.")
        pdf = document.tobytes()
    visited = []

    def upstream(request):
        url = urlsplit(request.url)
        visited.append(request.url)
        if url.hostname == "api.openalex.org":
            if url.path == "/works":
                return response(
                    request,
                    {
                        "results": [
                            {
                                "doi": "https://doi.org/10.1234/matched",
                                "title": title,
                                "type": "review",
                            }
                        ]
                    },
                )
            return response(
                request, {"locations": [{"pdf_url": "https://repo.org/paper.pdf"}]}
            )
        assert url.hostname in {"repo.org", "arxiv.org"}
        result = response(request, {})
        result._content = pdf
        result.headers["Content-Type"] = "application/pdf"
        return result

    transport(upstream)
    assert discovery.search("ARID3C")[0]["doi"] == "10.1234/matched"
    application = Application(Settings(data_dir=tmp_path))
    try:
        job = application.wait(
            application.submit(
                AcquisitionRequest(
                    identifiers=[
                        title,
                        "doi: 10.1234/direct",
                        "https://arxiv.org/abs/2301.12345v2",
                    ],
                    policy="legal_only",
                )
            ).id,
            10,
        )
        assert job.status == "succeeded"
        assert [item.identifier for item in job.results] == [
            "10.1234/matched",
            "10.1234/direct",
            "arxiv:2301.12345v2",
        ]
        assert all(
            application.store.artifact_path(item.artifacts[0].id).read_bytes() == pdf
            for item in job.results
        )
        assert "https://arxiv.org/pdf/2301.12345v2.pdf" in visited
    finally:
        application.close()


def test_arxiv_doi_is_looked_up_as_a_doi(transport):
    doi = "10.48550/arXiv.2301.12345"

    def upstream(request):
        assert urlsplit(request.url).hostname == "api.crossref.org"
        assert unquote(urlsplit(request.url).path) == f"/works/{doi}"
        return response(request, {}, status=404)

    transport(upstream)
    assert discovery.get_metadata(f"https://doi.org/{doi}", {}) == {
        "doi": doi,
        "metadata_source": "crossref",
        "metadata_status": "not_found",
    }


def test_http_mcp_metadata_share_metadata_and_failure_statuses(tmp_path, transport):
    def upstream(request):
        assert request.method == "GET"
        assert urlsplit(request.url).hostname == "api.crossref.org"
        assert unquote(urlsplit(request.url).path) == "/works/10.1234/known"
        return response(
            request,
            {
                "message": {
                    "DOI": "10.1234/known",
                    "title": ["A known study"],
                    "type": "journal-article",
                    "published": {"date-parts": [[2024]]},
                }
            },
        )

    transport(upstream)
    application = Application(Settings(data_dir=tmp_path), source=RecordingSource())
    with TestClient(create_app(application=application)) as client:
        http = client.post(
            "/api/v1/metadata", json={"doi": "https://doi.org/10.1234/known"}
        )
        assert http.status_code == 200
        assert http.json()["metadata_status"] == "found"
        assert http.json()["year"] == 2024
        assert mcp_metadata(client, "10.1234/known")["structuredContent"] == http.json()
        transport(lambda request: response(request, {}, status=503))
        partial = client.post("/api/v1/metadata", json={"doi": "10.1234/known"})
        assert partial.status_code == 200
        assert partial.json()["metadata_status"] == "unavailable"
        assert (
            mcp_metadata(client, "10.1234/known")["structuredContent"] == partial.json()
        )


def test_unexpected_programming_errors_are_not_metadata_unavailable(
    tmp_path, transport
):
    def broken(_request):
        raise RuntimeError("unexpected internal bug")

    transport(broken)
    with pytest.raises(RuntimeError, match="unexpected internal bug"):
        discovery.get_metadata("10.1234/known", {})
    application = Application(Settings(data_dir=tmp_path), source=RecordingSource())
    with TestClient(
        create_app(application=application), raise_server_exceptions=False
    ) as client:
        assert (
            client.post("/api/v1/metadata", json={"doi": "10.1234/known"}).status_code
            == 500
        )
        assert mcp_metadata(client, "10.1234/known")["isError"] is True


@pytest.mark.parametrize(
    "value",
    [
        "A paper title",
        "PMID:12345678",
        "PMC123456",
        "arxiv:2301.12345",
        "ISBN:9781234567890",
        " ",
    ],
)
def test_non_doi_metadata_input_is_rejected_before_network(tmp_path, transport, value):
    def unexpected(request):
        pytest.fail(f"Non-DOI input reached network: {request.url}")

    transport(unexpected)
    with pytest.raises(ValueError, match="requires a DOI"):
        discovery.get_metadata(value, {})
    application = Application(Settings(data_dir=tmp_path), source=RecordingSource())
    with TestClient(create_app(application=application)) as client:
        http = client.post("/api/v1/metadata", json={"doi": value})
        assert http.status_code == 400
        assert "requires a DOI" in http.json()["detail"]
        assert mcp_metadata(client, value)["isError"] is True
