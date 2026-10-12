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
from tests.academic_source.helpers import isolate_upstreams


@pytest.fixture(autouse=True)
def offline_upstreams(monkeypatch):
    isolate_upstreams(monkeypatch)


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
        if "api.openalex.org/works/doi:" in url:
            return Response(url)
        pytest.fail(f"Unexpected query/download before Europe PMC success: {url}")

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa("10.1128/MCB.22.19.6759-6766.2002", tmp_path, {})
    assert isinstance(result, SourceSuccess)
    assert len(visited) == 3
    assert [attempt.source for attempt in result.attempts] == [
        "Unpaywall",
        "OpenAlexOA",
        "EuropePMC",
        "EuropePMC",
    ]
    assert visited[-1] == "https://europepmc.org/api/getPdf?pmcid=PMC134036"


def test_progressive_metadata_chain_reaches_pmc_after_invalid_openalex_pdf(
    monkeypatch, tmp_path
):
    doi = "10.1234/paper"
    visited = []
    responses = []

    def fetch(url, config, **kwargs):
        visited.append(url)
        parsed = urlsplit(url)
        if parsed.netloc == "api.unpaywall.org":
            assert parsed.path == "/v2/10.1234%2Fpaper"
            assert parse_qs(parsed.query) == {"email": ["reader@university.edu"]}
            response = Response(url)
        elif parsed.netloc == "api.openalex.org":
            assert parsed.path == "/works/doi:10.1234%2Fpaper"
            response = Response(
                url, payload={"locations": [{"pdf_url": "https://repo.org/broken"}]}
            )
        elif url == "https://repo.org/broken":
            response = Response(url, body=b"%PDF-1.7\ntruncated")
        elif parsed.netloc == "www.ebi.ac.uk":
            assert parse_qs(parsed.query) == {
                "query": [f"DOI:{doi}"],
                "format": ["json"],
                "pageSize": ["5"],
                "resultType": ["core"],
            }
            response = Response(url, payload={"resultList": {"result": []}})
        elif parsed.netloc == "www.ncbi.nlm.nih.gov":
            assert parse_qs(parsed.query) == {"ids": [doi], "format": ["json"]}
            response = Response(url, payload={"records": [{"pmcid": "PMC134036"}]})
        else:
            assert url == "https://europepmc.org/api/getPdf?pmcid=PMC134036"
            response = Response(url, body=pdf_bytes())
        responses.append(response)
        return response

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa(doi, tmp_path, {"email": "reader@university.edu"})
    assert isinstance(result, SourceSuccess)
    assert [urlsplit(url).netloc for url in visited] == [
        "api.unpaywall.org",
        "api.openalex.org",
        "repo.org",
        "www.ebi.ac.uk",
        "www.ncbi.nlm.nih.gov",
        "europepmc.org",
    ]
    assert [attempt.source for attempt in result.attempts] == [
        "Unpaywall",
        "OpenAlexOA",
        "OpenAlexOA",
        "EuropePMC",
        "PMC",
        "EuropePMC",
    ]
    assert result.attempts[2].reason == "invalid_pdf"
    assert all(response.closed for response in responses)
    assert list(tmp_path.iterdir()) == [result.path]


def test_openalex_versioned_arxiv_pdf_stops_before_later_metadata(
    monkeypatch, tmp_path
):
    visited = []
    pdf = "https://arxiv.org/pdf/2401.01234v2.pdf"

    def fetch(url, config, **kwargs):
        visited.append(url)
        if "api.openalex.org" in url:
            return Response(
                url,
                payload={
                    "locations": [
                        {
                            "is_oa": True,
                            "pdf_url": pdf,
                            "landing_page_url": "https://arxiv.org/abs/2401.01234v2",
                        }
                    ]
                },
            )
        assert url == pdf
        return Response(url, body=pdf_bytes())

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa("10.1234/paper", tmp_path, {})
    assert isinstance(result, SourceSuccess)
    assert result.source == "OpenAlexOA"
    assert result.url == pdf
    assert len(visited) == 2


def test_duplicate_candidates_and_html_links_download_once_then_early_stop(
    monkeypatch, tmp_path
):
    visited = []
    landing = "https://repo.org/landing"
    api = "https://repo.org/api/file?id=42"

    def fetch(url, config, **kwargs):
        visited.append(url)
        if "api.unpaywall.org" in url:
            return Response(
                url,
                payload={
                    "best_oa_location": {"url_for_landing_page": landing},
                    "oa_locations": [{"url_for_landing_page": landing + "#section"}],
                },
            )
        if "api.openalex.org" in url:
            return Response(
                url,
                payload={
                    "locations": [
                        {"pdf_url": api},
                        {"pdf_url": "https://repo.org/good"},
                    ]
                },
            )
        if url == landing:
            return Response(
                url, body=f'<meta name="citation_pdf_url" content="{api}">'.encode()
            )
        if url == api:
            return Response(url, body=b"<html>Not a PDF</html>", mime="application/pdf")
        assert url == "https://repo.org/good"
        return Response(url, body=pdf_bytes())

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa(
        "10.1234/paper", tmp_path, {"email": "reader@university.edu"}
    )
    assert isinstance(result, SourceSuccess)
    assert len(visited) == 5
    assert [url for url in visited if "repo.org" in url] == [
        landing,
        api,
        "https://repo.org/good",
    ]
    assert result.source == "OpenAlexOA"
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
        assert url == "https://content.openalex.org/works/W42.pdf?api_key=key"
        return Response(url, body=pdf_bytes())

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa("10.1234/paper", tmp_path, {"openalex_api_key": "key"})
    assert isinstance(result, SourceSuccess)
    assert result.source == "OpenAlexContent"
    assert len(visited) == 2


@pytest.mark.parametrize("source", ["Unpaywall", "OpenAlexOA", "EuropePMC"])
def test_cdp_navigates_metadata_landing_and_reads_real_article_snapshot(
    monkeypatch, tmp_path, source
):
    from academic_source.sources.oa_candidates import (
        europepmc_candidates,
        openalex_candidates,
        unpaywall_candidates,
    )
    from scansci_pdf import browser_engine

    landing = "https://repository.example/articles/42"
    pdf = "https://repository.example/download?token=article-only"
    if source == "Unpaywall":
        candidates = unpaywall_candidates(
            {
                "best_oa_location": {
                    "url_for_pdf": None,
                    "url_for_landing_page": landing,
                }
            }
        )
    elif source == "OpenAlexOA":
        candidates = openalex_candidates(
            {"locations": [{"is_oa": True, "landing_page_url": landing}]}
        )
    else:
        candidates = europepmc_candidates(
            {
                "resultList": {
                    "result": [
                        {
                            "fullTextUrlList": {
                                "fullTextUrl": [
                                    {
                                        "url": landing,
                                        "documentStyle": "html",
                                        "availability": "Free",
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        )

    navigated = []
    closed = []

    class Page:
        url = "about:blank"

        def goto(self, url, **kwargs):
            self.url = url
            navigated.append(url)

        def content(self):
            if self.url == landing:
                return '<html><meta name="citation_pdf_url" content="/download?token=article-only"></html>'
            return "<html>Repository homepage</html>"

    page = Page()
    monkeypatch.setattr(
        oa,
        "discover",
        lambda *args: iter([(candidates, Attempt(source=source, status="discovered"))]),
    )
    monkeypatch.setattr(
        oa,
        "fetch",
        lambda *args, **kwargs: Response(landing, body=b"<html>reCAPTCHA</html>"),
    )
    monkeypatch.setattr(browser_engine, "create_tab", lambda *args, **kwargs: "owned")
    monkeypatch.setattr(
        browser_engine, "_resolve_tab", lambda tab: page if tab == "owned" else None
    )
    monkeypatch.setattr(
        browser_engine, "close_tab", lambda tab, config: closed.append(tab)
    )

    def fetch_pdf(tab, url, path, config):
        if url != pdf or page.url != landing:
            return False
        path.write_bytes(pdf_bytes())
        return True

    monkeypatch.setattr(browser_engine, "fetch_pdf_in_tab", fetch_pdf)
    result = oa.acquire_oa("10.1234/paper", tmp_path, {"browser_backend": "cdp"})
    assert isinstance(result, SourceSuccess)
    assert result.url == pdf
    assert landing in navigated
    assert set(closed) == {"owned"}


def test_landing_pdf_link_decodes_html_entities_without_changing_signed_query(
    monkeypatch, tmp_path
):
    from academic_source.sources.oa_candidates import unpaywall_candidates

    landing = "https://repository.example/articles/42"
    pdf = (
        "https://repository.example/article/file?type=printable&id=42&token=a%2Bb%2F%3D"
    )
    visited = []
    candidates = unpaywall_candidates(
        {"best_oa_location": {"url_for_landing_page": landing}}
    )
    monkeypatch.setattr(
        oa,
        "discover",
        lambda *args: iter(
            [(candidates, Attempt(source="Unpaywall", status="discovered"))]
        ),
    )

    def fetch(url, config, **kwargs):
        visited.append(url)
        if url == landing:
            return Response(
                url,
                body=b'<html><a href="/article/file?type=printable&amp;id=42&amp;token=a%2Bb%2F%3D">PDF</a></html>',
            )
        assert url == pdf
        return Response(url, body=pdf_bytes())

    monkeypatch.setattr(oa, "fetch", fetch)
    result = oa.acquire_oa("10.1234/paper", tmp_path, {})
    assert isinstance(result, SourceSuccess)
    assert result.url == pdf
    assert pdf in visited


def test_search_budget_exhaustion_allows_free_lookup_and_other_oa_sources(
    monkeypatch, tmp_path
):
    import json

    from scansci_pdf.search import search_papers
    from scansci_pdf.upstream_errors import RateLimited

    key = "fake-oa-budget-key"
    config = {"openalex_api_key": key}
    visited = []
    pdf = pdf_bytes()

    def send(_adapter, request, **kwargs):
        parsed = urlsplit(request.url)
        visited.append(request.url)
        result = requests.Response()
        result.status_code = 200
        result.request = request
        result.url = request.url
        result._content_consumed = True
        if parsed.hostname == "api.openalex.org":
            assert parse_qs(parsed.query)["api_key"] == [key]
            result.headers.update(
                {"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "3600"}
            )
            if parsed.path == "/works":
                result.status_code = 429
                payload = {"message": "private budget body"}
            else:
                payload = {
                    "id": "https://openalex.org/W42",
                    "open_access": {"is_oa": True},
                }
        elif parsed.hostname == "www.ebi.ac.uk":
            payload = {"resultList": {"result": [{"pmcid": "PMC134036"}]}}
        else:
            assert parsed.hostname == "europepmc.org"
            result._content = pdf
            result.headers["Content-Type"] = "application/pdf"
            return result
        result._content = json.dumps(payload).encode()
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(RateLimited):
        search_papers("papers", config=config)
    acquired = oa.acquire_oa("10.1234/limited", tmp_path, config)
    assert isinstance(acquired, SourceSuccess)
    assert acquired.source == "EuropePMC"
    assert acquired.path.read_bytes() == pdf
    content_attempt = next(
        item for item in acquired.attempts if item.source == "OpenAlexContent"
    )
    assert content_attempt.reason == "rate_limited"
    assert content_attempt.rate_limit["required"] == 100
    assert content_attempt.rate_limit["remaining"] == 5
    assert [urlsplit(url).hostname for url in visited] == [
        "api.openalex.org",
        "api.openalex.org",
        "www.ebi.ac.uk",
        "europepmc.org",
    ]


@pytest.mark.parametrize("entrypoint", ["progressive", "legacy"])
def test_openalex_content_authentication_reaches_real_http_adapter(
    monkeypatch, tmp_path, entrypoint
):
    import json

    from academic_source.interfaces.presentation import job_data
    from academic_source.services.application import Application
    from academic_source.settings import Settings
    from scansci_pdf.sources.openalex import try_openalex_content_api

    key = f"fake-{entrypoint}-key+with/reserved="
    email = "reader@example.test"
    config = {"openalex_api_key": key, "email": email, "json_probe_cache_seconds": 0}
    visited = []
    pdf = pdf_bytes().replace(b"%%EOF", b"\n" * 2000 + b"%%EOF")

    def send(_adapter, request, **kwargs):
        parsed = urlsplit(request.url)
        params = parse_qs(parsed.query)
        visited.append(parsed.hostname)
        result = requests.Response()
        result.status_code = 200
        result.request = request
        result.url = request.url
        result._content_consumed = True
        if parsed.hostname == "api.unpaywall.org":
            assert params["email"] == [email]
            result.status_code = 404
            result._content = b"{}"
        elif parsed.hostname == "api.openalex.org":
            assert params["api_key"] == [key]
            assert params["mailto"] == [email]
            result._content = json.dumps(
                {"id": "https://openalex.org/W42", "open_access": {"is_oa": True}}
            ).encode()
        else:
            assert parsed.hostname == "content.openalex.org"
            assert parsed.path == "/works/W42.pdf"
            assert params == {"api_key": [key]}
            result._content = pdf
            result.headers["Content-Type"] = "application/pdf"
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    if entrypoint == "progressive":
        application = Application(
            Settings(data_dir=tmp_path / "service", source_config=config)
        )
        try:
            request = AcquisitionRequest(
                identifiers=["10.1234/content"], policy="legal_only"
            )
            job = application.wait(application.submit(request).id, 10)
            assert job.status == "succeeded"
            artifact = job.results[0].artifacts[0]
            assert artifact.provenance.source == "OpenAlexContent"
            assert (
                artifact.provenance.url == "https://content.openalex.org/works/W42.pdf"
            )
            assert application.store.artifact_path(artifact.id).read_bytes() == pdf
            assert key not in application.job(job.id).model_dump_json()
            assert key not in str(job_data(job))
            cached = application.wait(application.submit(request).id, 10)
            assert cached.results[0].cached is True
            assert cached.results[0].artifacts[0].provenance == artifact.provenance
        finally:
            application.close()
    else:
        path = tmp_path / "content.pdf"
        acquired = try_openalex_content_api("10.1234/content", path, config)
        assert acquired["source"] == "OpenAlexContent"
        assert path.read_bytes() == pdf
    assert visited[-2:] == ["api.openalex.org", "content.openalex.org"]


def test_oa_metadata_limit_survives_source_aggregation_and_job_persistence(
    monkeypatch, tmp_path
):
    import json

    from academic_source.interfaces.presentation import job_data
    from academic_source.services.application import Application
    from academic_source.settings import Settings

    visited = []

    def send(_adapter, request, **kwargs):
        host = urlsplit(request.url).hostname
        visited.append(host)
        result = requests.Response()
        result.request = request
        result.url = request.url
        result._content_consumed = True
        result._content = json.dumps({"message": "private upstream body"}).encode()
        result.status_code = 429 if host == "api.openalex.org" else 404
        result.headers["Retry-After"] = "60"
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    # Bound this OA regression to the discovery chain, without publisher I/O.
    monkeypatch.setattr(sources, "_plan", lambda *args: [])
    application = Application(Settings(data_dir=tmp_path))
    try:
        job = application.wait(
            application.submit(
                AcquisitionRequest(identifiers=["10.1234/limited"], policy="legal_only")
            ).id,
            10,
        )
        assert job.status == "failed"
        failed = job.results[0]
        assert failed.reason == "rate_limited"
        assert failed.rate_limit["source"] == "openalex"
        assert failed.rate_limit["kind"] == "unknown"
        assert failed.action == "retry_later"
        assert visited == ["api.openalex.org", "www.ebi.ac.uk", "www.ncbi.nlm.nih.gov"]
        persisted = application.job(job.id)
        assert persisted.results[0].rate_limit == failed.rate_limit
        projected = job_data(persisted)["results"][0]
        assert projected["rate_limit"] == failed.rate_limit
        assert "retry at" in projected["message"]
        assert "private" not in str(projected)
    finally:
        application.close()


def test_content_failure_diagnostics_keep_public_url_without_credentials(
    monkeypatch, tmp_path
):
    import json

    key = "fake-content-diagnostics-key"

    def send(_adapter, request, **kwargs):
        result = requests.Response()
        result.request = request
        result.url = request.url
        result._content_consumed = True
        if urlsplit(request.url).hostname == "api.openalex.org":
            result.status_code = 200
            result._content = json.dumps(
                {"id": "https://openalex.org/W42", "open_access": {"is_oa": True}}
            ).encode()
        else:
            result.status_code = (
                403 if urlsplit(request.url).hostname == "content.openalex.org" else 404
            )
            result._content = b"{}"
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    failure = oa.acquire_oa("10.1234/content", tmp_path, {"openalex_api_key": key})
    assert isinstance(failure, SourceFailure)
    content_attempt = next(
        item for item in failure.attempts if item.source == "OpenAlexContent"
    )
    assert content_attempt.reason == "blocked"
    assert (
        content_attempt.message
        == "HTTP 403: https://content.openalex.org/works/W42.pdf"
    )
    assert key not in str(failure.attempts)


@pytest.mark.parametrize("entrypoint", ["oa", "content"])
def test_legacy_openalex_lookup_limit_reaches_safe_source_failure(
    monkeypatch, tmp_path, entrypoint
):
    from scansci_pdf.sources.openalex import try_openalex_content_api, try_openalex_oa

    key = f"fake-legacy-limit-{entrypoint}"
    fn = try_openalex_oa if entrypoint == "oa" else try_openalex_content_api

    def send(_adapter, request, **kwargs):
        assert parse_qs(urlsplit(request.url).query)["api_key"] == [key]
        result = requests.Response()
        result.request = request
        result.url = request.url
        result.status_code = 429
        result.headers["Retry-After"] = "60"
        result._content = b'{"message": "private raw body"}'
        result._content_consumed = True
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(sources, "_plan", lambda *args: [("OpenAlexOA", fn)])
    outcome = sources.LegacySources().acquire(
        "10.1234/limited",
        AcquisitionRequest(identifiers=["10.1234/limited"], policy="scihub_first"),
        tmp_path,
        {"openalex_api_key": key, "json_probe_cache_seconds": 0},
    )
    assert isinstance(outcome, SourceFailure)
    assert outcome.reason == "rate_limited"
    assert outcome.rate_limit["source"] == "openalex"
    assert outcome.action == "retry_later"
    assert "retry at" in outcome.message
    assert key not in outcome.message + str(outcome.attempts)
    assert "private raw body" not in outcome.message + str(outcome.attempts)


def test_content_cdp_success_keeps_credentials_out_of_public_and_cached_provenance(
    monkeypatch, tmp_path
):
    import json
    from urllib.parse import urlencode

    from fastapi.testclient import TestClient

    from academic_source.app import create_app
    from academic_source.services.application import Application
    from academic_source.settings import Settings
    from scansci_pdf import browser_backend, browser_engine
    from tests.providers.test_cdp_browser import FakePage

    key = "fake-cdp-key+with/reserved="
    encoded_key = urlencode({"api_key": key}).split("=", 1)[1]
    public_url = "https://content.openalex.org/works/W42.pdf"
    pdf = pdf_bytes()

    class ContentPage(FakePage):
        def goto(self, url, **kwargs):
            self.url = url

        def content(self):
            return "<html>OpenAlex Content</html>"

        def evaluate_handle(self, script, argument):
            self.result_url = argument["url"]
            assert parse_qs(urlsplit(self.result_url).query)["api_key"] == [key]
            return super().evaluate_handle(script, argument)

    page = ContentPage()
    page.payload = pdf

    def send(_adapter, request, **kwargs):
        parsed = urlsplit(request.url)
        result = requests.Response()
        result.request = request
        result.url = request.url
        result._content_consumed = True
        if parsed.hostname == "api.openalex.org":
            result.status_code = 200
            result._content = json.dumps(
                {"id": "https://openalex.org/W42", "open_access": {"is_oa": True}}
            ).encode()
        else:
            result.status_code = (
                403 if parsed.hostname == "content.openalex.org" else 404
            )
            if parsed.hostname == "content.openalex.org":
                assert parse_qs(parsed.query)["api_key"] == [key]
            result._content = b"{}"
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(browser_backend, "probe_cdp", lambda config: None)
    monkeypatch.setattr(browser_engine, "create_tab", lambda *args, **kwargs: "owned")
    monkeypatch.setattr(
        browser_engine, "_resolve_tab", lambda tab: page if tab == "owned" else None
    )
    monkeypatch.setattr(
        browser_engine, "close_tab", lambda *args, **kwargs: page.close()
    )
    application = Application(
        Settings(
            data_dir=tmp_path,
            auth_mode="off",
            source_config={"browser_backend": "cdp", "openalex_api_key": key},
        )
    )
    with TestClient(create_app(application=application)) as client:
        request = AcquisitionRequest(
            identifiers=["10.1234/content"], policy="legal_only"
        )
        job = application.wait(application.submit(request).id, 10)
        assert job.status == "succeeded"
        artifact = job.results[0].artifacts[0]
        assert artifact.provenance.source == "OpenAlexContent"
        assert artifact.provenance.url == public_url
        assert application.store.artifact_path(artifact.id).read_bytes() == pdf
        assert any(
            item.source == "OACDP" and item.status == "succeeded"
            for item in job.results[0].attempts
        )
        http = client.get(f"/api/v1/jobs/{job.id}")
        assert http.json()["artifacts"][0]["provenance"]["url"] == public_url
        assert key not in http.text and encoded_key not in http.text
        persisted = application.job(job.id).model_dump_json()
        assert key not in persisted and encoded_key not in persisted
        cached = application.wait(application.submit(request).id, 10)
        assert cached.results[0].cached is True
        assert cached.results[0].artifacts[0].provenance == artifact.provenance
        cached_record = application.job(cached.id).model_dump_json()
        assert key not in cached_record and encoded_key not in cached_record
