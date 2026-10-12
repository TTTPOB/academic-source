"""Observe single-source paper search through Python, HTTP, and MCP."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

from academic_source.app import create_app
from academic_source.services import discovery
from academic_source.services.application import Application
from academic_source.settings import Settings
from scansci_pdf import search as paper_search
from tests.academic_source.helpers import RecordingSource


def work(doi, title="A useful paper", **fields):
    return {
        "doi": f"https://doi.org/{doi}",
        "title": title,
        "type": "article",
        "publication_year": 2024,
        "publication_date": "2024-03-02",
        "cited_by_count": 3,
        "is_retracted": False,
        **fields,
    }


def response(request, payload, status=200):
    result = requests.Response()
    result.status_code = status
    result.request = request
    result.url = request.url
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    result.encoding = "utf-8"
    return result


@pytest.fixture
def transport(monkeypatch):
    """Replace only HTTP transport; execute the real discovery/services stack."""
    requests_seen = []

    def install(handler):
        def send(_session, request, **_kwargs):
            requests_seen.append(request)
            return handler(request)

        monkeypatch.setattr(requests.Session, "send", send)
        return requests_seen

    return install


@pytest.mark.parametrize(
    "search",
    [paper_search.search_papers, discovery.search],
)
def test_order_type_filter_and_limit_are_observable_for_each_search_entrypoint(
    search,
    transport,
):
    works = [
        work("10.1234/reviewer", type="peer-review"),
        work("10.1234/component", type="component"),
        work("10.1234/supplement", type="supplementary-materials"),
        work("10.1234/entry", type="reference-entry"),
        work("10.1234/data", type="dataset"),
        work("10.1234/paratext", type="paratext"),
        work("10.1234/abstract", type="conference-abstract"),
        work("10.1234/erratum", type="erratum"),
        work("10.1234/notice", type="retraction"),
        work("10.1234/retracted", is_retracted=True),
        work("10.1234/foreword", "Foreword"),
        work("10.1234/submission", "Manuscript Submission Guideline"),
        work("10.1234/review", "A Review of ARID3C", type="review", cited_by_count=1),
        work("10.1234/unknown", type="unknown", cited_by_count=100),
        work("10.1234/guideline", "Clinical Guideline for ARID3C", cited_by_count=2),
        work("10.1234/preprint", type="preprint"),
        work("10.1234/conference", type="conference-paper"),
    ]

    def upstream(request):
        if urlsplit(request.url).hostname == "api.openalex.org":
            return response(request, {"results": works})
        raise requests.Timeout("Metadata provider is unavailable")

    seen = transport(upstream)
    found = search("ARID3C", limit=5)
    assert [item["doi"] for item in found] == [
        "10.1234/review",
        "10.1234/unknown",
        "10.1234/guideline",
        "10.1234/preprint",
        "10.1234/conference",
    ]
    assert [item["type"] for item in found] == [
        "review",
        "unknown",
        "article",
        "preprint",
        "conference-paper",
    ]
    assert found[0]["is_retracted"] is False
    assert found[0]["publication_date"] == "2024-03-02"
    params = parse_qs(urlsplit(seen[0].url).query)
    assert params["search"] == ["ARID3C"]
    assert 5 < int(params["per_page"][0]) <= 200


def test_search_dates_missing_metadata_and_single_sided_years(transport):
    seen = transport(
        lambda request: response(
            request,
            {
                "results": [
                    work("10.1234/missing", publication_year="", publication_date=None),
                    work(
                        "10.1234/online",
                        publication_year="2025",
                        publication_date="2025-01-02",
                    ),
                ]
            },
        )
    )
    found = paper_search.search_papers(
        "online paper",
        year_from=2020,
        sort="publication_date:desc",
    )
    assert found[0]["year"] is None
    assert found[0]["publication_date"] is None
    assert found[1]["year"] == 2025
    params = parse_qs(urlsplit(seen[-1].url).query)
    assert params["filter"] == ["from_publication_date:2020-01-01"]
    assert params["sort"] == ["publication_date:desc"]
    paper_search.search_papers("online paper", year_to=2023, sort="cited_by_count")
    params = parse_qs(urlsplit(seen[-1].url).query)
    assert params["filter"] == ["to_publication_date:2023-12-31"]
    assert params["sort"] == ["cited_by_count:desc"]
    public = discovery.search("online paper")
    assert "year" not in public[0] and "publication_date" not in public[0]
    assert "abstract" not in public[0] and "oa_url" not in public[0]
    assert public[0]["is_oa"] is False


def test_candidate_window_is_bounded_and_does_not_promise_to_fill_limit(transport):
    seen = transport(
        lambda request: response(
            request,
            {
                "results": [
                    work("10.1234/one"),
                    work("10.1234/duplicate", type="peer-review"),
                ]
            },
        )
    )
    assert len(discovery.search("paper", limit=100)) == 1
    assert parse_qs(urlsplit(seen[0].url).query)["per_page"] == ["200"]
    for limit in (0, 101):
        with pytest.raises(ValueError, match="limit"):
            paper_search.search_papers("paper", limit=limit)
    with pytest.raises(ValueError, match="Sort"):
        paper_search.search_papers("paper", sort="made_up")


def test_zero_results_and_upstream_failures_are_distinct(transport):
    transport(lambda request: response(request, {"results": []}))
    assert discovery.search("nothing here") == []

    def unavailable(_request):
        raise requests.Timeout("test timeout")

    transport(unavailable)
    with pytest.raises(paper_search.DiscoveryError, match="OpenAlex.*network"):
        discovery.search("papers")
    transport(lambda request: response(request, {}, status=429))
    with pytest.raises(paper_search.DiscoveryError, match="HTTP 429"):
        discovery.search("papers")
    transport(lambda request: response(request, None))
    with pytest.raises(paper_search.DiscoveryError, match="invalid results"):
        discovery.search("papers")

    def invalid_json(request):
        result = response(request, {})
        result._content = b"not JSON"
        return result

    transport(invalid_json)
    with pytest.raises(paper_search.DiscoveryError, match="invalid JSON"):
        discovery.search("papers")


def test_title_resolution_matches_evidence_not_first_hit_and_rejects_ambiguity(
    transport,
):
    from scansci_pdf.resolver import resolve_title_to_doi

    title = "The role of ARID3C in cancer biology"
    transport(
        lambda request: response(
            request,
            {
                "results": [
                    work("10.1234/unrelated", "A completely unrelated publication"),
                    work("10.1234/matched", title),
                ]
            },
        )
    )
    assert resolve_title_to_doi(title, {}) == "10.1234/matched"
    assert paper_search.search_by_title(title)["doi"] == "10.1234/matched"

    transport(
        lambda request: response(
            request,
            {
                "results": [
                    work("10.1234/unrelated", "A completely unrelated publication"),
                ]
            },
        )
    )
    assert resolve_title_to_doi(title, {}) is None
    transport(
        lambda request: response(
            request,
            {
                "results": [
                    work("10.1234/one", title),
                    work("10.1234/two", title),
                ]
            },
        )
    )
    assert resolve_title_to_doi(title, {}) is None
    transport(lambda request: response(request, {}, status=503))
    with pytest.raises(paper_search.DiscoveryError, match="OpenAlex"):
        resolve_title_to_doi(title, {})


def mcp_call(client, name, arguments):
    return client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ).json()["result"]


def test_http_mcp_search_share_type_projection_validation_and_errors(
    tmp_path, transport
):
    transport(
        lambda request: response(
            request,
            {
                "results": [
                    work("10.1234/peer", type="peer-review"),
                    work("10.1234/review", type="review"),
                ]
            },
        )
    )
    application = Application(Settings(data_dir=tmp_path), source=RecordingSource())
    with TestClient(create_app(application=application)) as client:
        http = client.get("/api/v1/search", params={"query": "papers", "limit": 1})
        assert http.status_code == 200
        assert http.json()[0]["type"] == "review"
        mcp = mcp_call(client, "search", {"query": "papers", "limit": 1})
        assert mcp["structuredContent"]["result"] == http.json()
        assert client.get("/api/v1/search", params={"query": " "}).status_code == 400
        invalid = mcp_call(client, "search", {"query": "papers", "limit": 101})
        assert invalid["isError"] is True

        transport(lambda request: response(request, {"results": []}))
        assert client.get("/api/v1/search", params={"query": "nothing"}).json() == []
        transport(lambda request: response(request, {}, status=503))
        failed = client.get("/api/v1/search", params={"query": "papers"})
        assert failed.status_code == 502
        assert "OpenAlex" in failed.json()["detail"]
        failed_mcp = mcp_call(client, "search", {"query": "papers"})
        assert failed_mcp["isError"] is True
        assert "OpenAlex" in failed_mcp["content"][0]["text"]
