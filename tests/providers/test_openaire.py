"""OpenAIRE source: URL extraction from nested payloads + XML fallback."""

from __future__ import annotations

from scansci_pdf.sources.openaire import (
    _extract_from_xml,
    extract_openaire_fulltext_urls,
)


def test_extracts_nested_webresource_urls():
    payload = {
        "response": {
            "results": {
                "result": [
                    {
                        "metadata": {"title": "X"},
                        "webresource": [
                            {"url": "https://repo.example.org/123/article.pdf"}
                        ],
                    },
                    {
                        "children": {"url": "https://repo.example.org/456/landing"},
                    },
                ]
            }
        }
    }
    urls = extract_openaire_fulltext_urls(payload)
    assert urls == [
        "https://repo.example.org/123/article.pdf",
        "https://repo.example.org/456/landing",
    ]


def test_xml_fallback_extracts_webresources():
    xml = "<result><webresource><url>https://repo.example.org/a.pdf</url></webresource></result>"
    assert _extract_from_xml(xml) == ["https://repo.example.org/a.pdf"]
