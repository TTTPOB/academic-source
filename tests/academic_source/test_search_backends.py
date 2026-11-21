"""Exercise native Crossref and PubMed searches with offline HTTP responses."""

import json
from contextlib import nullcontext
from urllib.parse import parse_qs, urlsplit

import pytest

from academic_source import client as remote
from academic_source.interfaces import cli
from academic_source.services import discovery
from tests.academic_source import test_discovery, test_search_filters
from tests.academic_source.test_discovery import mcp_call, response
from tests.academic_source.test_search_filters import params

offline_upstreams = test_discovery.offline_upstreams
transport = test_discovery.transport
server = test_search_filters.server


def form(request):
    assert request.method == "POST"
    return {name: values[0] for name, values in parse_qs(request.body).items()}


def article(pmid, doi=None):
    article_id = f'<ArticleId IdType="doi">{doi}</ArticleId>' if doi else ""
    return f"""
    <PubmedArticle>
      <MedlineCitation>
        <PMID>{pmid}</PMID>
        <Article>
          <ArticleTitle>A <i>test</i> article</ArticleTitle>
          <Journal><JournalIssue><PubDate><Year>2020</Year><Month>Feb</Month><Day>3</Day></PubDate></JournalIssue><Title>Test Journal</Title></Journal>
          <AuthorList><Author><LastName>Lovelace</LastName><ForeName>Ada</ForeName></Author></AuthorList>
          <Abstract><AbstractText Label="METHODS">Full <b>abstract</b>.</AbstractText></Abstract>
          <PublicationTypeList><PublicationType>Editorial</PublicationType></PublicationTypeList>
        </Article>
      </MedlineCitation>
      <PubmedData><ArticleIdList>{article_id}</ArticleIdList></PubmedData>
    </PubmedArticle>
    """


def xml_response(request, records):
    result = response(request, {})
    result._content = (
        f"<PubmedArticleSet>{''.join(records)}</PubmedArticleSet>".encode()
    )
    return result


@pytest.mark.parametrize("backend", ["crossref", "pubmed"])
def test_native_parameters_resolve_journal_name_and_preserve_filter(transport, backend):
    raw = (
        " from-online-pub-date:2026-10-01 , type:journal-article "
        if backend == "crossref"
        else " 2026/10/01:2026/11/20[epdat] OR (News[pt]) "
    )

    def upstream(request):
        path = urlsplit(request.url).path
        if path == "/sources":
            return response(
                request,
                {
                    "results": [
                        {
                            "id": "S2",
                            "display_name": "Journal Group",
                            "alternate_titles": ["Test Journal"],
                            "issn_l": "1111-2222",
                        },
                        {
                            "id": "S1",
                            "display_name": "Test Journal",
                            "issn_l": "0123-456X",
                        },
                    ]
                },
            )
        if backend == "crossref":
            assert path == "/works"
            return response(request, {"message": {"items": []}})
        assert path.endswith("/esearch.fcgi")
        return response(request, {"esearchresult": {"idlist": []}})

    seen = transport(upstream)
    assert (
        discovery.search(
            "  test OR data  ",
            limit=1000,
            backend=backend,
            journal="test journal",
            year="2020-2024",
            author="Ada Lovelace",
            sort="publication_date",
            type="journal-article,book"
            if backend == "crossref"
            else "Journal Article,Review",
            filter=raw,
            config={"email": "author@example.org"},
        )
        == []
    )
    assert len(seen) == 2 and params(seen[0])["search"] == "test journal"
    if backend == "crossref":
        assert params(seen[1]) == {
            "query": "test OR data",
            "query.author": "Ada Lovelace",
            "sort": "issued",
            "order": "desc",
            "rows": "100",
            "offset": "0",
            "mailto": "author@example.org",
            "filter": "issn:0123-456X,from-pub-date:2020-01-01,until-pub-date:2024-12-31,type:journal-article,type:book,"
            + raw,
        }
    else:
        assert form(seen[1]) == {
            "db": "pubmed",
            "retmax": "1000",
            "retmode": "json",
            "sort": "pub date",
            "tool": "academic-source",
            "term": "(test OR data) AND (0123-456X[issn]) AND (2020:2024[dp]) AND (Ada Lovelace[au]) AND (Journal Article[pt] OR Review[pt]) AND ("
            + raw
            + ")",
        }


@pytest.mark.parametrize(
    ("year", "crossref", "pubmed"),
    [
        ("2020", "from-pub-date:2020-01-01,until-pub-date:2020-12-31", "2020[dp]"),
        ("2020-", "from-pub-date:2020-01-01", "2020:9999[dp]"),
        ("-2024", "until-pub-date:2024-12-31", "0001:2024[dp]"),
    ],
)
def test_single_and_open_ended_years(transport, year, crossref, pubmed):
    def upstream(request):
        if request.method == "POST":
            assert form(request)["term"] == pubmed
            return response(request, {"esearchresult": {"idlist": []}})
        assert params(request)["filter"] == crossref
        return response(request, {"message": {"items": []}})

    seen = transport(upstream)
    for backend in ("crossref", "pubmed"):
        assert discovery.search(backend=backend, year=year) == []
    assert len(seen) == 2


@pytest.mark.parametrize("backend", ["crossref", "pubmed"])
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"oa": True}, "oa"),
        ({"min_citations": 0}, "min_citations"),
        ({"in_": "title"}, "search scope"),
        ({"sort": "cited_by_count"}, "publication_date"),
        ({"limit": 1001}, "between 1 and 1000"),
    ],
)
def test_unsupported_filters_fail_before_network(transport, backend, options, message):
    transport(lambda request: pytest.fail(f"Unexpected request: {request.url}"))
    with pytest.raises(ValueError, match=message):
        discovery.search("test", backend=backend, **options)


def test_pubmed_ascending_date_rejected(transport):
    transport(lambda request: pytest.fail(f"Unexpected request: {request.url}"))
    with pytest.raises(ValueError, match="only supports descending publication_date"):
        discovery.search("test", backend="pubmed", sort="publication_date:asc")


@pytest.mark.pubmed
@pytest.mark.parametrize("sort", [None, "publication_date:asc"])
def test_crossref_pages_by_offset_and_fills_missing_abstract(transport, sort):
    def work(number, **fields):
        return {
            "DOI": f"10.1234/{number}",
            "abstract": "<jats:title>Abstract</jats:title><jats:p>Full &amp; clean.</jats:p>",
            **fields,
        }

    def upstream(request):
        path = urlsplit(request.url).path
        if path == "/works":
            sent = params(request)
            assert "cursor" not in sent
            assert sent["rows"] == "100" and sent["filter"] == "issn:0028-0836"
            assert sent["query"] == "test"
            if sort is None:
                assert "sort" not in sent and "order" not in sent
            else:
                assert sent["sort"] == "issued" and sent["order"] == "asc"
            if sent["offset"] == "0":
                items = [work(i) for i in range(100)]
                items[0].update(
                    title=["Foreword"],
                    type="book",
                    issued={"date-parts": [[2023, 1, 11]]},
                    **{"is-referenced-by-count": 0},
                )
            else:
                assert sent["offset"] == "100"
                items = [work(100, abstract=None)]
            return response(request, {"message": {"items": items}})
        sent = form(request)
        if path.endswith("/esearch.fcgi"):
            assert sent["term"] == '"10.1234/100"[doi]'
            return response(request, {"esearchresult": {"idlist": ["77"]}})
        assert path.endswith("/efetch.fcgi") and sent["id"] == "77"
        return xml_response(request, [article("77", "10.1234/100")])

    seen = transport(upstream)
    found = discovery.search(
        "test", backend="crossref", journal="0028-0836", limit=105, sort=sort
    )
    assert len(found) == 101 and len(seen) == 4
    assert found[0] == {
        "doi": "10.1234/0",
        "title": "Foreword",
        "type": "book",
        "year": 2023,
        "publication_date": "2023-01-11",
        "cited_by_count": 0,
        "source": "crossref",
        "abstract": "Full & clean.",
    }
    assert found[-1] == {
        "doi": "10.1234/100",
        "source": "crossref",
        "abstract": "METHODS: Full abstract.",
    }


def test_pubmed_fetches_in_batches_and_preserves_pmid_results_without_doi(transport):
    pmids = [str(i) for i in range(1, 102)]
    raw = "  2026/11/12[epdat]  "

    def upstream(request):
        sent = form(request)
        if urlsplit(request.url).path.endswith("/esearch.fcgi"):
            assert sent["term"] == raw and sent["retmax"] == "101"
            return response(request, {"esearchresult": {"idlist": pmids}})
        batch = sent["id"].split(",")
        return xml_response(
            request,
            [
                article(pmid, "10.1234/test" if pmid == "101" else None)
                for pmid in reversed(batch)
            ],
        )

    seen = transport(upstream)
    found = discovery.search(backend="pubmed", filter=raw, limit=101)
    assert [form(req)["id"].split(",") for req in seen[1:]] == [
        pmids[:100],
        pmids[100:],
    ]
    assert [item["pmid"] for item in found] == pmids
    assert found[0] == {
        "pmid": "1",
        "title": "A test article",
        "year": 2020,
        "type": "Editorial",
        "publication_date": "2020-02-03",
        "source": "pubmed",
        "authors": ["Ada Lovelace"],
        "url": "https://pubmed.ncbi.nlm.nih.gov/1/",
        "journal": "Test Journal",
        "abstract": "METHODS: Full abstract.",
    }
    assert all("doi" not in item for item in found[:-1])
    assert found[-1]["doi"] == "10.1234/test"


@pytest.mark.parametrize("backend", ["crossref", "pubmed"])
def test_native_filter_rejection_reaches_all_four_entries(
    server, transport, monkeypatch, capsys, backend
):
    raw = "invalid:term" if backend == "crossref" else "(invalid[term]"
    detail = "Upstream rejected the native filter."

    def reject(request):
        if backend == "crossref":
            assert params(request)["filter"] == raw
            return response(
                request,
                {
                    "status": "failed",
                    "message": [{"type": "validation", "message": detail}],
                },
                status=400,
            )
        assert form(request)["term"] == raw
        return response(request, {"error": detail}, status=400)

    seen = transport(reject)
    options = {"backend": backend, "filter": raw}
    response_http = server.get("/api/v1/search", params=options)
    assert response_http.status_code == 400 and response_http.json()["detail"] == detail
    mcp = mcp_call(server, "search", options)
    assert mcp["isError"] and detail in mcp["content"][0]["text"]
    with pytest.raises(ValueError) as error:
        remote.search(server, **options)
    assert str(error.value) == detail
    monkeypatch.setattr(remote, "open_client", lambda *_args: nullcontext(server))
    assert (
        cli.main(
            [
                "search",
                "--backend",
                backend,
                "--filter",
                raw,
                "--server",
                "http://testserver",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.err.strip() == detail and captured.out == ""
    assert len(seen) == 4


@pytest.mark.parametrize(
    ("term", "fields", "ids", "diagnostic"),
    [
        (
            "lamin AND )",
            {"warninglist": {"outputmessages": ["AND", ")"]}},
            ["1"],
            "AND\n)",
        ),
        (
            '"lamin protein qzxwvq"',
            {"warninglist": {"quotedphrasesnotfound": ['"lamin protein qzxwvq"']}},
            ["1"],
            'quotedphrasesnotfound: "lamin protein qzxwvq"',
        ),
        (
            "lamin OR qzxwvq[ti]",
            {"errorlist": {"phrasesnotfound": ["qzxwvq"]}},
            ["1"],
            "phrasesnotfound: qzxwvq",
        ),
        (
            "the OR lamin",
            {"warninglist": {"phrasesignored": ["the"]}},
            ["1"],
            "phrasesignored: the",
        ),
        (
            "lamin[xyz]",
            {"errorlist": {"fieldsnotfound": ["xyz"]}},
            [],
            "fieldsnotfound: xyz",
        ),
        (
            "lamin AND qzxwvq",
            {
                "errorlist": {"phrasesnotfound": ["qzxwvq"]},
                "warninglist": {"outputmessages": ["No items found."]},
            },
            [],
            None,
        ),
        (
            '"kinase receptor qzxwvq"[ti]',
            {
                "warninglist": {
                    "quotedphrasesnotfound": ['"kinase receptor qzxwvq"[ti]'],
                    "outputmessages": ["No items found."],
                }
            },
            [],
            None,
        ),
    ],
)
def test_pubmed_reports_query_diagnostics_but_not_ordinary_empty_results(
    server,
    transport,
    term,
    fields,
    ids,
    diagnostic,
):
    translation = '"lamin"[All Fields]'

    def upstream(request):
        assert urlsplit(request.url).path.endswith("/esearch.fcgi")
        assert form(request)["term"] == term
        return response(
            request,
            {
                "esearchresult": {
                    "idlist": ids,
                    "querytranslation": translation,
                    **fields,
                }
            },
        )

    seen = transport(upstream)
    result = server.get("/api/v1/search", params={"backend": "pubmed", "filter": term})
    if diagnostic:
        assert result.status_code == 400
        assert (
            result.json()["detail"] == f"{diagnostic}\nquerytranslation: {translation}"
        )
    else:
        assert result.status_code == 200 and result.json() == []
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("article_date", "expected"),
    [
        (
            '<ArticleDate DateType="Electronic"><Year>2019</Year><Month>12</Month><Day>30</Day></ArticleDate>',
            (2019, "2019-12-30"),
        ),
        (
            '<ArticleDate DateType="Electronic"><Year>2019</Year><Month>12</Month></ArticleDate>',
            (2020, "2020-02-03"),
        ),
        (
            '<ArticleDate DateType="Print"><Year>2019</Year><Month>12</Month><Day>30</Day></ArticleDate>',
            (2020, "2020-02-03"),
        ),
    ],
)
def test_pubmed_dates_prefer_complete_electronic_metadata(
    transport, article_date, expected
):
    def upstream(request):
        if urlsplit(request.url).path.endswith("/esearch.fcgi"):
            return response(request, {"esearchresult": {"idlist": ["1"]}})
        record = article("1").replace("</Article>", f"{article_date}</Article>")
        return xml_response(request, [record])

    transport(upstream)
    (item,) = discovery.search(backend="pubmed", filter="2020[dp]")
    assert (item["year"], item["publication_date"]) == expected


def test_local_cli_selects_pubmed_and_passes_exact_results(
    transport, server, monkeypatch, capsys
):
    seen = transport(
        lambda request: response(request, {"esearchresult": {"idlist": []}})
    )
    monkeypatch.delenv("ACADEMIC_SOURCE_SERVER", raising=False)
    monkeypatch.setattr(cli, "_application", lambda: server.app.state.application)
    assert cli.main(["search", "--backend", "pubmed", "--filter", "test[pt]"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert len(seen) == 1 and form(seen[0])["term"] == "test[pt]"
