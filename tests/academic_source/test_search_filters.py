"""Exercise the OpenAlex search contract without external network access."""

import json
from contextlib import nullcontext
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from academic_source import client as remote
from academic_source.app import create_app
from academic_source.interfaces import cli
from academic_source.services import discovery
from academic_source.services.application import Application
from academic_source.settings import Settings
from scansci_pdf.upstream_errors import RateLimited
from tests.academic_source import test_discovery
from tests.academic_source.helpers import RecordingSource
from tests.academic_source.test_discovery import mcp_call, response, work

# Share the OpenAlex transport and isolation fixtures with the existing tests.
offline_upstreams = test_discovery.offline_upstreams
transport = test_discovery.transport


def params(request):
    return {
        key: values[0] for key, values in parse_qs(urlsplit(request.url).query).items()
    }


@pytest.fixture
def server(tmp_path):
    app = Application(
        Settings(data_dir=tmp_path, auth_mode="off"), source=RecordingSource()
    )
    with TestClient(create_app(application=app)) as client:
        yield client


def test_http_mcp_client_and_cli_send_same_filters(
    server, transport, monkeypatch, capsys
):
    seen = transport(
        lambda request: response(
            request,
            {
                "results": [
                    work(
                        "10.1234/example",
                        primary_location={"source": {"display_name": "Nature"}},
                    )
                ]
            },
        )
    )
    options = {
        "query": "  materials  ",
        "limit": 2,
        "journal": "0028-0836",
        "year": "2020-2024",
        "type": "article, review",
        "oa": True,
        "min_citations": 50,
        "author": "https://openalex.org/A123",
        "sort": "publication_date:asc",
        "in": "abstract",
        "filter": "language:!es|!fr,cited_by_count:<1000",
    }
    http = server.get("/api/v1/search", params={**options, "per_page": 1000})
    assert http.status_code == 200
    found = http.json()
    assert found[0]["journal"] == "Nature"
    assert mcp_call(server, "search", options)["structuredContent"]["result"] == found
    listing = server.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ).json()["result"]["tools"]
    schema = next(tool["inputSchema"] for tool in listing if tool["name"] == "search")
    assert schema["properties"].keys() == options.keys() | {"backend"}
    assert schema["properties"]["backend"]["default"] == "openalex"
    assert "query" not in schema.get("required", [])
    assert remote.search(server, **options) == found
    monkeypatch.setattr(remote, "open_client", lambda *_args: nullcontext(server))
    assert (
        cli.main(
            [
                "search",
                "materials",
                "--server",
                "http://testserver",
                "--limit",
                "2",
                "--journal",
                "0028-0836",
                "--year",
                "2020-2024",
                "--type",
                "article, review",
                "--oa",
                "--min_citations",
                "50",
                "--author",
                "https://openalex.org/A123",
                "--sort",
                "publication_date:asc",
                "--in",
                "abstract",
                "--filter",
                options["filter"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == found
    assert len(seen) == 4
    for request in seen:
        assert (urlsplit(request.url).hostname, urlsplit(request.url).path) == (
            "api.openalex.org",
            "/works",
        )
        assert params(request) == {
            "per_page": "30",
            "search.title_and_abstract": "materials",
            "sort": "publication_date:asc",
            "filter": (
                "from_publication_date:2020-01-01,to_publication_date:2024-12-31,"
                "type:article|review,open_access.is_oa:true,cited_by_count:>49,"
                "authorships.author.id:A123,primary_location.source.issn:0028-0836,"
                "is_retracted:false,language:!es|!fr,cited_by_count:<1000"
            ),
        }


@pytest.mark.parametrize(
    ("year", "date_filters"),
    [
        (
            "2021",
            ["from_publication_date:2021-01-01", "to_publication_date:2021-12-31"],
        ),
        ("2020-", ["from_publication_date:2020-01-01"]),
        ("-2024", ["to_publication_date:2024-12-31"]),
    ],
)
def test_filter_only_year_search_defaults_to_newest(transport, year, date_filters):
    seen = transport(lambda request: response(request, {"results": []}))
    assert discovery.search(year=year, min_citations=0) == []
    sent = params(seen[0])
    assert not any(key.startswith("search") for key in sent)
    assert sent["sort"] == "publication_date:desc"
    assert [
        part for part in sent["filter"].split(",") if "publication_date:" in part
    ] == date_filters
    assert "cited_by_count:>-1" in sent["filter"].split(",")


def test_missing_query_or_invalid_typed_filter_fails_before_network(transport):
    seen = transport(lambda request: pytest.fail(f"Unexpected request: {request.url}"))
    for options in ({}, {"sort": "publication_date"}, {"oa": False}):
        with pytest.raises(ValueError, match="query or at least one filter"):
            discovery.search(" ", **options)
    with pytest.raises(ValueError, match="starting year"):
        discovery.search(year="2024-2020")
    with pytest.raises(ValueError, match="Year must be YYYY"):
        discovery.search(year="2020-20")
    with pytest.raises(ValueError, match="non-negative"):
        discovery.search(min_citations=-1)
    assert not seen


def test_explicit_types_and_raw_retraction_override_default_exclusions(transport):
    seen = transport(
        lambda request: response(
            request,
            {
                "results": [
                    work("10.1234/peer", type="peer-review"),
                    work("10.1234/withdrawn", type="peer-review", is_retracted=True),
                ]
            },
        )
    )
    assert discovery.search("papers") == []
    filters = params(seen[-1])["filter"].split(",")
    assert "is_retracted:false" in filters
    assert {v for v in filters if v.startswith("type:")} == {
        f"type:!{kind}"
        for kind in (
            "book",
            "book-chapter",
            "conference-abstract",
            "dataset",
            "erratum",
            "paratext",
            "peer-review",
            "reference-entry",
            "retraction",
            "standard",
            "supplementary-materials",
        )
    }
    assert "sort" not in params(seen[-1])
    assert [r["doi"] for r in discovery.search(type="peer-review")] == ["10.1234/peer"]
    assert params(seen[-1])["filter"] == "type:peer-review,is_retracted:false"
    raw = "type:peer-review,is_retracted:true"
    assert len(discovery.search(filter=raw)) == 2
    assert params(seen[-1])["filter"] == raw


@pytest.mark.parametrize(
    ("author", "clause"),
    [
        ("A123", "authorships.author.id:A123"),
        ("https://orcid.org/0000-0002-1825-009X", "author.orcid:0000-0002-1825-009X"),
        ("Ada Lovelace", "authorships.author.id:A456"),
    ],
)
def test_author_always_combines_with_the_query(transport, author, clause):
    def upstream(request):
        if urlsplit(request.url).path == "/authors":
            assert params(request)["search"] == "Ada Lovelace"
            return response(
                request,
                {
                    "results": [
                        {
                            "id": "https://openalex.org/A123",
                            "display_name": "Ada Love",
                            "cited_by_count": 900,
                        },
                        {
                            "id": "https://openalex.org/A456",
                            "display_name": "Ada Lovelace",
                        },
                    ]
                },
            )
        return response(request, {"results": [work("10.1234/author")]})

    seen = transport(upstream)
    assert discovery.search("computing", author=author)[0]["doi"] == "10.1234/author"
    assert len(seen) == (2 if author == "Ada Lovelace" else 1)
    assert params(seen[-1])["search"] == "computing"
    assert clause in params(seen[-1])["filter"].split(",")


def test_journal_resolves_one_exact_alternate_or_abbreviated_title(transport):
    def upstream(request):
        if urlsplit(request.url).path == "/sources":
            sent = params(request)
            assert sent["search"] == "J TEST"
            assert "cursor" not in sent
            return response(
                request,
                {
                    "results": [
                        {
                            "id": "S1",
                            "display_name": "J Testing",
                            "issn_l": "0123-456X",
                        },
                        {
                            "id": "https://openalex.org/S2",
                            "display_name": "Journal of Testing",
                            "alternate_titles": ["J Test"],
                            "abbreviated_title": "j test",
                        },
                    ]
                },
            )
        return response(request, {"results": []})

    seen = transport(upstream)
    assert discovery.search(journal="J TEST") == []
    assert len(seen) == 2
    assert "primary_location.source.id:S2" in params(seen[-1])["filter"].split(",")


def test_journal_prefers_the_source_named_exactly(transport):
    sources = [
        {
            "id": "S2",
            "display_name": "Developmental Cell",
            "alternate_titles": ["Cell"],
        },
        {"id": "S1", "display_name": "Cell"},
    ]

    def upstream(request):
        if urlsplit(request.url).path == "/sources":
            return response(request, {"results": sources})
        return response(request, {"results": []})

    seen = transport(upstream)
    discovery.search(journal="cell")
    assert "primary_location.source.id:S1" in params(seen[-1])["filter"].split(",")


@pytest.mark.parametrize("ambiguous", [True, False])
def test_journal_failure_lists_at_most_ten_candidates(transport, ambiguous):
    candidates = [
        {
            "id": f"S{i}",
            "display_name": f"Journal {i:02d}",
            "issn_l": f"0000-1{i:03d}",
            "abbreviated_title": "JT" if ambiguous else None,
        }
        for i in range(12)
    ]
    seen = transport(lambda request: response(request, {"results": candidates}))
    with pytest.raises(
        ValueError, match="ambiguous" if ambiguous else "no exact"
    ) as error:
        discovery.search(journal="jt")
    assert len(seen) == 1 and urlsplit(seen[0].url).path == "/sources"
    message = str(error.value)
    assert "Retry with an ISSN" in message
    assert "Journal 00 (ISSN-L: 0000-1000)" in message
    assert "Journal 09" in message and "Journal 10" not in message


@pytest.mark.parametrize("scope", ["title", "abstract"])
def test_scoped_search_uses_search_budget(transport, scope):
    def reject(request):
        rejected = response(request, {}, status=429)
        rejected.headers["X-RateLimit-Remaining"] = "5"
        rejected.headers["X-RateLimit-Reset"] = "3600"
        return rejected

    seen = transport(reject)
    with pytest.raises(RateLimited, match="required=10"):
        discovery.search("materials", in_=scope)
    expected = {"title": "search.title", "abstract": "search.title_and_abstract"}
    assert params(seen[0])[expected[scope]] == "materials"


def test_upstream_bad_filter_error_reaches_all_transports(
    server,
    transport,
    monkeypatch,
    capsys,
):
    raw = "unknown:!whatever"
    detail = "unknown is not a valid field."

    def reject(request):
        assert params(request)["filter"].endswith("," + raw)
        return response(request, {"error": "Bad query", "message": detail}, status=400)

    transport(reject)
    http = server.get("/api/v1/search", params={"filter": raw})
    assert http.status_code == 400 and http.json()["detail"] == detail
    mcp = mcp_call(server, "search", {"filter": raw})
    assert mcp["isError"] and detail in mcp["content"][0]["text"]
    monkeypatch.setattr(remote, "open_client", lambda *_args: nullcontext(server))
    assert cli.main(["search", "--filter", raw, "--server", "http://testserver"]) == 1
    assert capsys.readouterr().err.strip() == detail
