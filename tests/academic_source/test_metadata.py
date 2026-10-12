"""Observe explicitly sourced metadata separately from discovery and acquisition."""

import json
from urllib.parse import parse_qs, unquote, urlsplit

import pymupdf
import pytest
import requests
from fastapi.testclient import TestClient

from academic_source.app import create_app
from academic_source.domain import AcquisitionRequest
from academic_source.services import discovery
from academic_source.services.application import Application
from academic_source.settings import Settings
from tests.academic_source.helpers import (
    RecordingSource,
    isolate_upstreams,
    paper_bytes,
)


@pytest.fixture(autouse=True)
def offline_upstreams(monkeypatch):
    isolate_upstreams(monkeypatch)


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


def mcp_metadata(client, identifier, id_type="doi"):
    return client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_metadata",
                "arguments": {
                    "id_type": id_type,
                    "doi" if id_type == "doi" else "arxiv_id": identifier,
                },
            },
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
    result = discovery.get_metadata("doi", {}, doi=f"doi: {doi}")
    assert result == {
        "id_type": "doi",
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
    assert discovery.get_metadata("doi", {}, doi="10.1234/minimal") == {
        "id_type": "doi",
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
    result = discovery.get_metadata("doi", {}, doi="https://doi.org/10.1234/known")
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
    assert discovery.get_metadata("doi", {}, doi=f"https://doi.org/{doi}") == {
        "id_type": "doi",
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
    application = Application(
        Settings(data_dir=tmp_path, auth_mode="off"), source=RecordingSource()
    )
    with TestClient(create_app(application=application)) as client:
        http = client.post(
            "/api/v1/metadata",
            json={"doi": "https://doi.org/10.1234/known", "id_type": "doi"},
        )
        assert http.status_code == 200
        assert http.json()["metadata_status"] == "found"
        assert http.json()["year"] == 2024
        assert mcp_metadata(client, "10.1234/known")["structuredContent"] == http.json()
        transport(lambda request: response(request, {}, status=503))
        partial = client.post(
            "/api/v1/metadata", json={"doi": "10.1234/known", "id_type": "doi"}
        )
        assert partial.status_code == 200
        assert partial.json()["metadata_status"] == "unavailable"
        assert (
            mcp_metadata(client, "10.1234/known")["structuredContent"] == partial.json()
        )


@pytest.mark.parametrize(
    "identifier,id_type", [("10.1234/known", "doi"), ("1706.03762v1", "arxiv")]
)
def test_unexpected_programming_errors_are_not_metadata_unavailable(
    tmp_path, transport, identifier, id_type
):
    def broken(_request):
        raise RuntimeError("unexpected internal bug")

    transport(broken)
    with pytest.raises(RuntimeError, match="unexpected internal bug"):
        discovery.get_metadata(
            id_type, {}, **{"doi" if id_type == "doi" else "arxiv_id": identifier}
        )
    application = Application(
        Settings(data_dir=tmp_path, auth_mode="off"), source=RecordingSource()
    )
    with TestClient(
        create_app(application=application), raise_server_exceptions=False
    ) as client:
        assert (
            client.post(
                "/api/v1/metadata",
                json={
                    "doi" if id_type == "doi" else "arxiv_id": identifier,
                    "id_type": id_type,
                },
            ).status_code
            == 500
        )
        assert mcp_metadata(client, identifier, id_type)["isError"] is True


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
        discovery.get_metadata("doi", {}, doi=value)
    application = Application(
        Settings(data_dir=tmp_path, auth_mode="off"), source=RecordingSource()
    )
    with TestClient(create_app(application=application)) as client:
        http = client.post("/api/v1/metadata", json={"doi": value, "id_type": "doi"})
        assert http.status_code == 400
        assert "requires a DOI" in http.json()["detail"]
        assert mcp_metadata(client, value)["isError"] is True


def arxiv_response(request, record_id="1706.03762v3", *, body=None, status=200):
    result = response(request, {}, status)
    result._content = (
        body
        if body is not None
        else f"""<feed xmlns="http://www.w3.org/2005/Atom"
            xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <id>http://arxiv.org/abs/{record_id}</id>
            <title> Attention Is
 All You Need </title>
            <author><name>Ashish Vaswani</name></author>
            <author><name>Research Group</name></author>
            <summary> An abstract
 with details. </summary>
            <published>2017-06-12T17:57:34Z</published>
            <updated>2023-08-02T00:00:00Z</updated>
            <arxiv:doi>https://doi.org/10.1234/published</arxiv:doi>
          </entry>
        </feed>"""
    ).encode()
    return result


@pytest.mark.parametrize(
    "value, requested_id, actual_id",
    [
        ("1706.03762", "1706.03762", "1706.03762v3"),
        ("arxiv:1706.03762v1", "1706.03762v1", "1706.03762v1"),
        ("https://arxiv.org/abs/1706.03762v1", "1706.03762v1", "1706.03762v1"),
        (
            "https://arxiv.org/pdf/hep-th/9901001v2.pdf",
            "hep-th/9901001v2",
            "hep-th/9901001v2",
        ),
        ("hep-th/9901001", "hep-th/9901001", "hep-th/9901001v2"),
        ("https://doi.org/10.48550/arXiv.1706.03762v1", "1706.03762v1", "1706.03762v1"),
    ],
)
def test_arxiv_identity_normalization_and_versioned_metadata(
    transport, value, requested_id, actual_id
):
    def upstream(request):
        url = urlsplit(request.url)
        assert request.method == "GET"
        assert (url.scheme, url.hostname, url.path) == (
            "https",
            "export.arxiv.org",
            "/api/query",
        )
        assert parse_qs(url.query) == {"id_list": [requested_id]}
        return arxiv_response(request, actual_id)

    transport(upstream)
    assert discovery.get_metadata("arxiv", {}, arxiv_id=value) == {
        "id_type": "arxiv",
        "arxiv_id": requested_id,
        "metadata_source": "arxiv",
        "metadata_status": "found",
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani", "Research Group"],
        "year": 2017,
        "abstract": "An abstract with details.",
        "published": "2017-06-12T17:57:34Z",
        "updated": "2023-08-02T00:00:00Z",
        "version": int(actual_id.rsplit("v", 1)[1]),
        "published_doi": "10.1234/published",
    }


@pytest.mark.parametrize(
    "requested, expected", [("1706.03762", "found"), ("1706.03762v1", "unavailable")]
)
def test_versionless_entry_cannot_confirm_an_explicit_version(
    transport, requested, expected
):
    transport(
        lambda request: arxiv_response(
            request,
            body="""
        <feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>http://arxiv.org/abs/1706.03762</id>
        </entry></feed>""",
        )
    )
    result = discovery.get_metadata("arxiv", {}, arxiv_id=requested)
    assert result["metadata_status"] == expected
    assert result["arxiv_id"] == requested
    assert "version" not in result
    assert "published_doi" not in result


@pytest.mark.parametrize(
    "failure",
    [
        "empty",
        "error",
        "mismatch",
        "version",
        "invalid_id",
        "non_atom",
        "xml",
        "http",
        "network",
    ],
)
def test_arxiv_absence_and_upstream_failures_are_distinct(transport, failure):
    def upstream(request):
        if failure == "network":
            raise requests.Timeout("private proxy/key must not appear in response")
        if failure == "http":
            return arxiv_response(request, status=503)
        if failure == "empty":
            return arxiv_response(
                request, body='<feed xmlns="http://www.w3.org/2005/Atom"/>'
            )
        if failure == "xml":
            return arxiv_response(request, body="<feed>")
        if failure == "non_atom":
            return arxiv_response(request, body="<html/>")
        record_id = {
            "error": "http://arxiv.org/api/errors#incorrect_id_format",
            "mismatch": "http://arxiv.org/abs/1706.99999v1",
            "version": "http://arxiv.org/abs/1706.03762v3",
            "invalid_id": "http://arxiv.org/abs/not-an-id",
        }[failure]
        return arxiv_response(
            request,
            body=f"""
            <feed xmlns="http://www.w3.org/2005/Atom"><entry>
              <id>{record_id}</id><title>Error or wrong record</title>
            </entry></feed>""",
        )

    transport(upstream)
    result = discovery.get_metadata("arxiv", {}, arxiv_id="1706.03762v1")
    assert result["arxiv_id"] == "1706.03762v1"
    assert result["metadata_source"] == "arxiv"
    assert result["metadata_status"] == (
        "not_found" if failure == "empty" else "unavailable"
    )
    if failure != "empty":
        assert "arXiv" in result["metadata_error"]
    assert "private" not in str(result)
    assert "title" not in result


@pytest.mark.parametrize(
    "value",
    [
        "A title",
        "PMID:12345",
        "10.1234/article",
        "prefix 10.48550/arXiv.1706.03762",
        "1706.03762v0",
    ],
)
def test_invalid_arxiv_reference_is_rejected_before_network(tmp_path, transport, value):
    def unexpected(request):
        pytest.fail(f"Invalid input reached network: {request.url}")

    transport(unexpected)
    with pytest.raises(ValueError, match="requires an arXiv ID"):
        discovery.get_metadata("arxiv", {}, arxiv_id=value)
    application = Application(
        Settings(data_dir=tmp_path, auth_mode="off"), source=RecordingSource()
    )
    with TestClient(create_app(application=application)) as client:
        http = client.post(
            "/api/v1/metadata", json={"arxiv_id": value, "id_type": "arxiv"}
        )
        assert http.status_code == 400
        assert mcp_metadata(client, value, "arxiv")["isError"] is True


def test_arxiv_http_and_mcp_use_application_network_settings(tmp_path, monkeypatch):
    proxy = "http://private:password@proxy.example:9876"
    monkeypatch.delenv("SCANSCI_PDF_PROXY", raising=False)
    unavailable = False

    def send(_session, request, **kwargs):
        assert kwargs["timeout"] == (4, 11)
        assert kwargs["proxies"] == {"http": proxy, "https": proxy}
        assert "ScanSci" in request.headers["User-Agent"]
        assert request.headers["Accept"] == "application/atom+xml"
        assert parse_qs(urlsplit(request.url).query) == {"id_list": ["1706.03762v1"]}
        if unavailable:
            raise requests.Timeout(f"private configuration: {proxy}")
        return arxiv_response(request, "1706.03762v1")

    monkeypatch.setattr(requests.Session, "send", send)
    application = Application(
        Settings(
            data_dir=tmp_path,
            auth_mode="off",
            source_config={
                "network_proxy": proxy,
                "connect_timeout": 4,
                "read_timeout": 11,
            },
        ),
        source=RecordingSource(),
    )
    with TestClient(create_app(application=application)) as client:
        payload = {"arxiv_id": "1706.03762v1", "id_type": "arxiv"}
        http = client.post("/api/v1/metadata", json=payload)
        assert http.status_code == 200
        assert http.json()["metadata_status"] == "found"
        assert (
            mcp_metadata(client, payload["arxiv_id"], "arxiv")["structuredContent"]
            == http.json()
        )
        assert "private" not in http.text
        unavailable = True
        failed = client.post("/api/v1/metadata", json=payload)
        assert failed.json()["metadata_status"] == "unavailable"
        assert (
            mcp_metadata(client, payload["arxiv_id"], "arxiv")["structuredContent"]
            == failed.json()
        )
        assert "private" not in failed.text


@pytest.mark.parametrize(
    "arguments",
    [
        {"id_type": "doi"},
        {"id_type": "arxiv"},
        {"id_type": "doi", "arxiv_id": "1706.03762"},
        {"id_type": "arxiv", "doi": "10.1234/known"},
        {"id_type": "doi", "doi": "10.1234/known", "arxiv_id": "1706.03762"},
        {"id_type": "arxiv", "doi": "10.1234/known", "arxiv_id": "1706.03762"},
    ],
)
def test_metadata_requires_only_the_selected_type_field(tmp_path, transport, arguments):
    def unexpected(request):
        pytest.fail(f"Invalid metadata arguments reached network: {request.url}")

    transport(unexpected)
    with pytest.raises(ValueError, match="requires .* only"):
        discovery.get_metadata(config={}, **arguments)
    application = Application(
        Settings(data_dir=tmp_path, auth_mode="off"), source=RecordingSource()
    )
    with TestClient(create_app(application=application)) as client:
        http = client.post("/api/v1/metadata", json=arguments)
        assert http.status_code == 400
        assert "requires" in http.json()["detail"]
        mcp = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_metadata", "arguments": arguments},
            },
        ).json()["result"]
        assert mcp["isError"] is True


@pytest.mark.parametrize("id_type,source", [("doi", "crossref"), ("arxiv", "arxiv")])
def test_http_mcp_metadata_limit_keeps_source_and_safe_retry_information(
    tmp_path, monkeypatch, id_type, source
):
    from datetime import datetime

    identifier = "10.1234/limited" if id_type == "doi" else "1706.03762v1"
    field = "doi" if id_type == "doi" else "arxiv_id"
    proxy = f"http://private:fake-key@metadata-limit-{source}.test:8080"

    def send(_adapter, request, **kwargs):
        result = response(request, {"message": f"private upstream body {proxy}"}, 429)
        result.headers["Retry-After"] = "60"
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    application = Application(
        Settings(
            data_dir=tmp_path, auth_mode="off", source_config={"network_proxy": proxy}
        ),
        source=RecordingSource(),
    )
    with TestClient(create_app(application=application)) as client:
        http = client.post(
            "/api/v1/metadata", json={"id_type": id_type, field: identifier}
        )
        assert http.status_code == 200
        data = http.json()
        assert data["metadata_status"] == "unavailable"
        assert data["metadata_source"] == source
        assert data[field] == identifier
        assert "HTTP 429" in data["metadata_error"]
        assert data["rate_limit"]["source"] == source
        retry = datetime.fromisoformat(data["rate_limit"]["retry_at"])
        assert retry.utcoffset().total_seconds() == 0

        def rejected_network(_adapter, request, **kwargs):
            pytest.fail("A metadata request in cooldown reached HTTP transport")

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", rejected_network)
        mcp = mcp_metadata(client, identifier, id_type)
        assert mcp["structuredContent"] == data
        assert "private" not in http.text + str(mcp)
        assert "fake-key" not in http.text + str(mcp)


def test_title_limit_is_persisted_and_projected_while_direct_doi_still_succeeds(
    tmp_path, monkeypatch
):
    from academic_source.interfaces.presentation import job_data

    title = "The role of ARID3C in cancer biology"
    key = "fake-title-budget-key"

    def send(_adapter, request, **kwargs):
        assert urlsplit(request.url).hostname == "api.openalex.org"
        result = response(request, {"message": "private body"}, 429)
        result.headers.update(
            {"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "3600"}
        )
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    application = Application(
        Settings(
            data_dir=tmp_path, auth_mode="off", source_config={"openalex_api_key": key}
        ),
        source=RecordingSource(),
    )
    with TestClient(create_app(application=application)) as client:
        job = application.wait(
            application.submit(
                AcquisitionRequest(
                    identifiers=[title, "10.1234/direct"], policy="legal_only"
                )
            ).id,
            10,
        )
        assert job.status == "partial"
        failed, succeeded = job.results
        assert failed.reason == "rate_limited"
        assert failed.action == "retry_later"
        assert failed.rate_limit["kind"] == "daily_budget"
        assert failed.rate_limit["remaining"] == 5
        assert failed.rate_limit["required"] == 10
        assert succeeded.status == "succeeded"
        persisted = application.job(job.id)
        assert persisted.results[0].rate_limit == failed.rate_limit
        http = client.get(f"/api/v1/jobs/{job.id}")
        projected = http.json()["results"][0]
        assert projected["rate_limit"] == failed.rate_limit
        assert projected["message"] == failed.message
        assert "rate_limit" not in http.json()["results"][1]
        assert projected == job_data(persisted)["results"][0]
        assert key not in http.text
