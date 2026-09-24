"""Elsevier API request shape and preview rejection without network access."""

from types import SimpleNamespace

import pymupdf
import pytest

from scansci_pdf import _publisher_strategies_core as publisher
from scansci_pdf.sources import elsevier_api

DOI = "10.1016/j.cell.2025.03.048"
MAIN_EID = "1-s2.0-S0092867425001234-main.pdf"
XML = f"""<?xml version="1.0"?>
<full-text-retrieval-response>
  <coredata><title>Cell paper</title></coredata>
  <attachment><attachment-eid>1-s2.0-S0092867425001234-mmc1.pdf</attachment-eid>
    <description>supplementary</description></attachment>
  <attachment><attachment-eid>{MAIN_EID}</attachment-eid>
    <description>main full text</description></attachment>
</full-text-retrieval-response>"""
ARTICLE_ONLY_XML = """<?xml version="1.0"?>
<full-text-retrieval-response><coredata>
  <eid>1-s2.0-S0092867425001234</eid>
</coredata></full-text-retrieval-response>"""


def _pdf(pages):
    with pymupdf.open() as document:
        for _ in range(pages):
            document.new_page().insert_text((72, 72), "full text " * 100)
        return document.tobytes()


def test_default_xml_yields_main_attachment_and_fulltext(monkeypatch):
    calls = []

    def request(url, headers, **kwargs):
        calls.append((url, headers["Accept"], kwargs))
        return SimpleNamespace(status_code=200, text=XML)

    monkeypatch.setattr(elsevier_api, "_api_request", request)
    assert elsevier_api._fetch_attachment_eids(DOI, "example-only")[0] == MAIN_EID
    assert elsevier_api.fetch_fulltext(DOI, "example-only")["title"] == "Cell paper"
    assert len(calls) == 2
    assert all(
        url.endswith(DOI) and accept == "application/xml" and not kwargs
        for url, accept, kwargs in calls
    )


@pytest.mark.parametrize("object_status", [200, 401])
def test_publisher_rejects_preview_and_uses_default_xml_object_chain(
    monkeypatch, tmp_path, object_status
):
    calls = []
    preview = _pdf(1)
    full_pdf = _pdf(2)

    class Session:
        trust_env = False

        def get(self, url, **kwargs):
            accept = kwargs["headers"]["Accept"]
            calls.append((url, accept, kwargs.get("params")))
            if "/object/eid/" in url:
                return SimpleNamespace(
                    status_code=object_status,
                    headers={"content-type": "application/pdf"},
                    content=full_pdf if object_status == 200 else b"",
                )
            if accept == "application/pdf":
                return SimpleNamespace(
                    status_code=200,
                    headers={"content-type": "application/pdf"},
                    content=preview,
                )
            if accept == "application/xml":
                xml = XML if object_status == 200 else ARTICLE_ONLY_XML
                return SimpleNamespace(
                    status_code=200,
                    headers={"content-type": "application/xml"},
                    content=xml.encode(),
                    text=xml,
                )
            return SimpleNamespace(status_code=404, headers={}, content=b"")

    monkeypatch.setattr("requests.Session", Session)
    monkeypatch.setattr(publisher, "_persist_api_cookies", lambda *args: 0)
    output = tmp_path / "paper.pdf"
    result = publisher.try_elsevier_api(
        DOI, output, {"elsevier_api_key": "example-only", "min_pdf_size_bytes": 0}
    )
    assert calls[0][1] == "application/pdf"
    assert calls[1][1] == "application/xml" and calls[1][2] is None
    assert "/object/eid/" in calls[2][0] and "main.pdf" in calls[2][0]
    assert all(not params or "view" not in params for _, _, params in calls)
    if object_status == 200:
        assert result and result["success"] and output.read_bytes() == full_pdf
    else:
        assert result is None and not output.exists()
