import json
from urllib.parse import parse_qs, urlsplit

import pytest
from requests import HTTPError, Response

from scansci_pdf.pmc_cloud import (
    BUCKET_URL,
    europepmc_pdf_url,
    find_article,
    lookup_pmcid,
)

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def response(url, body, status=200):
    result = Response()
    result.url = url
    result.status_code = status
    result._content = body if isinstance(body, bytes) else body.encode()
    result._content_consumed = True
    return result


def listing(keys, token=None):
    contents = "".join(
        f"<Contents><Key>{key}</Key><Size>{size}</Size></Contents>"
        for key, size in keys
    )
    more = (
        f"<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>"
        if token
        else "<IsTruncated>false</IsTruncated>"
    )
    return f'<ListBucketResult xmlns="{S3_NS}">{more}{contents}</ListBucketResult>'


def test_lookup_pmcid_matches_the_requested_doi():
    calls = []

    def fetch(url):
        calls.append(url)
        records = [
            {"doi": "10.1038/S41586-021-03819-2", "pmcid": "PMC8371605"},
        ]
        return response(url, json.dumps({"records": records}))

    assert lookup_pmcid(" 10.1038/s41586-021-03819-2 ", fetch) == "PMC8371605"
    query = parse_qs(urlsplit(calls[0]).query)
    assert query["ids"] == ["10.1038/s41586-021-03819-2"]
    assert query["format"] == ["json"]


def test_lookup_pmcid_returns_none_when_pmc_lacks_the_article():
    def fetch(url):
        record = {
            "doi": "10.1093/bioinformatics/btz076",
            "status": "error",
            "errmsg": "Identifier not found in PMC",
        }
        return response(url, json.dumps({"records": [record]}))

    assert lookup_pmcid("10.1093/bioinformatics/btz076", fetch) is None


def test_lookup_pmcid_raises_on_http_error():
    with pytest.raises(HTTPError):
        lookup_pmcid("10.1/x", lambda url: response(url, "", status=503))


def test_find_article_picks_the_latest_version_and_reports_sizes():
    keys = [
        ("PMC8371605.1/PMC8371605.1.pdf", 100),
        ("PMC8371605.1/old.xlsx", 5),
        ("PMC8371605.2/PMC8371605.2.pdf", 200),
        ("PMC8371605.2/PMC8371605.2.xml", 30),
        ("PMC8371605.2/PMC8371605.2.json", 3),
        ("PMC8371605.2/PMC8371605.2.txt", 4),
        ("PMC8371605.2/MOESM1 data.pdf", 50),
        ("PMC8371605.2/Fig1_HTML.jpg", 7),
    ]
    article = find_article("PMC8371605", lambda url: response(url, listing(keys)))

    assert article is not None
    assert article.version == 2
    assert article.pdf is not None and article.pdf.size == 200
    assert article.xml is not None and article.xml.name == "PMC8371605.2.xml"
    assert [file.name for file in article.attachments] == [
        "MOESM1 data.pdf",
        "Fig1_HTML.jpg",
    ]
    assert article.attachments[0].url == f"{BUCKET_URL}/PMC8371605.2/MOESM1%20data.pdf"


def test_find_article_follows_continuation_tokens():
    calls = []

    def fetch(url):
        calls.append(parse_qs(urlsplit(url).query))
        if len(calls) == 1:
            return response(url, listing([("PMC1.1/a.csv", 1)], token="next"))
        return response(url, listing([("PMC1.1/b.csv", 2)]))

    article = find_article("PMC1", fetch)

    assert article is not None
    assert [file.name for file in article.files] == ["a.csv", "b.csv"]
    assert calls[0]["prefix"] == ["PMC1."]
    assert calls[1]["continuation-token"] == ["next"]


def test_find_article_returns_none_for_an_empty_listing():
    assert find_article("PMC1", lambda url: response(url, listing([]))) is None


def test_author_manuscript_has_no_pdf():
    keys = [("PMC6687398.1/PMC6687398.1.xml", 9), ("PMC6687398.1/PMC6687398.1.txt", 3)]
    article = find_article("PMC6687398", lambda url: response(url, listing(keys)))

    assert article is not None
    assert article.pdf is None
    assert article.attachments == ()
    assert europepmc_pdf_url("PMC6687398") == (
        "https://europepmc.org/api/getPdf?pmcid=PMC6687398"
    )
