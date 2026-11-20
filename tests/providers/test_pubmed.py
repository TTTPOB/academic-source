from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from scansci_pdf import pubmed, search

pytestmark = pytest.mark.pubmed

EFETCH = b"""<PubmedArticleSet>
<PubmedArticle><MedlineCitation><Article><Abstract>
<AbstractText Label="BACKGROUND">Cells <i>divide</i>.</AbstractText>
<AbstractText Label="RESULTS">Genes   matter.</AbstractText>
</Abstract></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">1</ArticleId>
<ArticleId IdType="doi">10.1/A</ArticleId></ArticleIdList></PubmedData></PubmedArticle>
<PubmedArticle><MedlineCitation><Article></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/b</ArticleId></ArticleIdList></PubmedData></PubmedArticle>
</PubmedArticleSet>"""


def fake_eutils(monkeypatch, calls, fail=False):
    def send(_adapter, request, **kwargs):
        url = urlsplit(request.url)
        assert request.method == "POST" and not url.query
        calls.append((url.path.rsplit("/", 1)[-1], parse_qs(request.body)))
        result = requests.Response()
        result.request = request
        result.url = request.url
        result.status_code = 503 if fail else 200
        if url.path.endswith("esearch.fcgi"):
            result._content = b'{"esearchresult": {"idlist": ["1", "2"]}}'
        else:
            result._content = EFETCH
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)


def test_abstracts_are_matched_by_doi_and_keep_section_labels(monkeypatch):
    calls = []
    fake_eutils(monkeypatch, calls)
    found = pubmed.pubmed_abstracts(["10.1/a", "10.1/B", "10.1/a"], {})
    assert found == {"10.1/a": "BACKGROUND: Cells divide. RESULTS: Genes matter."}
    assert [name for name, _ in calls] == ["esearch.fcgi", "efetch.fcgi"]
    assert calls[0][1]["term"] == ['"10.1/a"[doi] OR "10.1/b"[doi]']


def test_batches_and_failures(monkeypatch):
    calls = []
    fake_eutils(monkeypatch, calls)
    pubmed.pubmed_abstracts([f"10.1/{i}" for i in range(150)], {})
    assert [name for name, _ in calls].count("esearch.fcgi") == 2

    calls.clear()
    fake_eutils(monkeypatch, calls, fail=True)
    assert pubmed.pubmed_abstracts(["10.1/a"], {}) == {}


def test_search_fills_only_missing_abstracts(monkeypatch):
    asked = []
    monkeypatch.setattr(
        pubmed,
        "pubmed_abstracts",
        lambda dois, config: asked.extend(dois) or {"10.1/b": "From PubMed."},
    )
    results = [
        {"doi": "10.1/a", "abstract": "From OpenAlex."},
        {"doi": "10.1/B", "abstract": ""},
    ]
    search._fill_abstracts(results, {})
    assert asked == ["10.1/B"]
    assert [item["abstract"] for item in results] == ["From OpenAlex.", "From PubMed."]
