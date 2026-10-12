"""Observe on-demand Crossref metadata separately from OpenAlex discovery."""

import json
from urllib.parse import unquote, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

from academic_source.app import create_app
from academic_source.services import discovery
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import RecordingSource


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


def mcp_resolve(client, identifier):
    return client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "resolve", "arguments": {"identifier": identifier}},
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
    result = discovery.resolve(f"doi: {doi}", {})
    assert result == {
        "identifier": doi,
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
    assert discovery.resolve("10.1234/minimal", {}) == {
        "identifier": "10.1234/minimal",
        "doi": "10.1234/minimal",
        "metadata_status": "found",
        "metadata_source": "crossref",
    }


@pytest.mark.parametrize("failure", ["404", "503", "network", "json", "mismatched_doi"])
def test_metadata_failure_is_explicit_without_discarding_normalized_doi(
    failure, transport
):
    def upstream(request):
        if failure == "network":
            raise requests.Timeout("could not reach provider")
        if failure in {"404", "503"}:
            return response(request, {}, status=int(failure))
        if failure == "json":
            result = response(request, {})
            result._content = b"invalid JSON"
            return result
        return response(request, {"message": {"DOI": "10.1234/some-other-work"}})

    transport(upstream)
    result = discovery.resolve("https://doi.org/10.1234/known", {})
    assert result["identifier"] == result["doi"] == "10.1234/known"
    assert result["metadata_status"] == (
        "not_found" if failure == "404" else "unavailable"
    )
    if failure != "404":
        assert "Crossref" in result["metadata_error"]
    assert "title" not in result


def test_search_and_title_matching_do_not_depend_on_crossref_metadata(transport):
    title = "The role of ARID3C in cancer biology"

    def upstream(request):
        if urlsplit(request.url).hostname == "api.crossref.org":
            raise requests.Timeout("metadata outage")
        assert urlsplit(request.url).hostname == "api.openalex.org"
        return response(
            request,
            {
                "results": [
                    {
                        "doi": "https://doi.org/10.1234/known",
                        "title": title,
                        "type": "review",
                        "publication_year": 2024,
                    }
                ]
            },
        )

    transport(upstream)
    assert discovery.search("ARID3C")[0]["doi"] == "10.1234/known"
    assert discovery.resolve(title, {}) == {
        "identifier": "10.1234/known",
        "doi": "10.1234/known",
        "title": title,
    }
    assert discovery.resolve("10.1234/known", {})["metadata_status"] == "unavailable"
    assert discovery.resolve("https://arxiv.org/abs/2301.12345v2", {}) == {
        "identifier": "arxiv:2301.12345v2",
        "doi": None,
    }


def test_http_mcp_resolve_share_metadata_and_failure_statuses(tmp_path, transport):
    transport(
        lambda request: response(
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
    )
    application = Application(Settings(data_dir=tmp_path), source=RecordingSource())
    with TestClient(create_app(application=application)) as client:
        http = client.post("/api/v1/resolve", json={"identifier": "doi: 10.1234/known"})
        assert http.status_code == 200
        assert http.json()["metadata_status"] == "found"
        assert http.json()["year"] == 2024
        assert mcp_resolve(client, "10.1234/known")["structuredContent"] == http.json()
        transport(lambda request: response(request, {}, status=503))
        partial = client.post("/api/v1/resolve", json={"identifier": "10.1234/known"})
        assert partial.status_code == 200
        assert partial.json()["metadata_status"] == "unavailable"
        assert (
            mcp_resolve(client, "10.1234/known")["structuredContent"] == partial.json()
        )
        failed_title = client.post(
            "/api/v1/resolve", json={"identifier": "A title whose search fails"}
        )
        assert failed_title.status_code == 502
        assert "OpenAlex" in failed_title.json()["detail"]
        assert mcp_resolve(client, "A title whose search fails")["isError"] is True
        assert (
            client.post("/api/v1/resolve", json={"identifier": " "}).status_code == 400
        )


def test_unexpected_programming_errors_are_not_metadata_unavailable(transport):
    def broken(_request):
        raise RuntimeError("unexpected internal bug")

    transport(broken)
    with pytest.raises(RuntimeError, match="unexpected internal bug"):
        discovery.resolve("10.1234/known", {})
