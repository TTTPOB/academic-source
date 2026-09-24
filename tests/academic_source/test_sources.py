"""Source-policy regression tests without contacting scholarly services."""

from __future__ import annotations

from importlib import import_module

import pymupdf
import pytest

from academic_source.domain import AcquisitionRequest
from academic_source.sources import LegacySources


def _pdf(path):
    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()


def _patch_handlers(monkeypatch, visited, *, win=None):
    """Replace site I/O, keeping the real planner and execution order in use."""
    from scansci_pdf.sources import publishers

    def handler(label):
        def run(doi, path, config):
            visited.append(label)
            if label == win:
                _pdf(path)
                return {"file": str(path), "source": label, "success": True}
            return {"success": False, "error_type": "not_found"}

        return run

    monkeypatch.setattr(
        publishers,
        "get_publisher_fast_sources",
        lambda doi: [(handler("PublisherDirect"), "PublisherDirect")],
    )
    for module, funcs in {
        "unpaywall": {"try_unpaywall": "Unpaywall"},
        "openalex": {
            "try_openalex_oa": "OpenAlexOA",
            "try_openalex_content_api": "OpenAlexContent",
        },
        "semantic_scholar": {"try_semanticscholar": "SemanticScholar"},
        "openaire": {"try_openaire": "OpenAIRE"},
        "oa_discovery": {"try_doaj": "DOAJ"},
        "crossref": {"try_crossref_page_scrape": "CrossrefPage"},
        "europepmc": {"try_europepmc": "EuropePMC", "try_pmc": "PMC"},
        "core_api": {"try_core": "CORE"},
        "scibban": {"try_scibban": "SciBban"},
        "libgen": {"try_libgen": "LibGen"},
        "scihub": {"try_scihub": "Sci-Hub"},
        "carsi_source": {"try_carsi": "CARSI"},
        "vpnsci": {"try_vpnsci": "WebVPN"},
        "ezproxy": {"try_ezproxy": "EZProxy"},
    }.items():
        target = import_module("scansci_pdf.sources." + module)
        for func, label in funcs.items():
            monkeypatch.setattr(target, func, handler(label))
    return handler


def test_explicit_arxiv_version_reaches_download_url(monkeypatch, tmp_path):
    from scansci_pdf.sources import arxiv

    requested = []

    def fetch(url, path, config):
        requested.append(url)
        _pdf(path)
        return {"file": str(path), "source": "arXiv", "success": True}

    monkeypatch.setattr(arxiv, "download_arxiv_pdf", fetch)
    result = LegacySources().acquire(
        "arxiv:2401.01234v2",
        AcquisitionRequest(identifiers=["arxiv:2401.01234v2"]),
        tmp_path,
        {},
    )
    assert requested == ["https://arxiv.org/pdf/2401.01234v2.pdf"]
    assert result["source"] == "arXiv"


def _get(tmp_path, policy, config):
    return LegacySources().acquire(
        "10.1234/paper",
        AcquisitionRequest(identifiers=["10.1234/paper"], policy=policy),
        tmp_path,
        config,
    )


def test_legal_only_exhausts_legal_sources_without_grey(monkeypatch, tmp_path):
    visited = []
    _patch_handlers(monkeypatch, visited)
    result = _get(tmp_path, "legal_only", {"scihub_enabled": True})
    assert result["success"] is False
    assert {"SemanticScholar", "OpenAIRE", "CORE", "CrossrefPage", "PMC"} <= set(
        visited
    )
    assert set(visited).isdisjoint({"SciBban", "LibGen", "Sci-Hub"})
    assert len(result["attempts"]) == len(visited)


def test_disable_grey_respected_even_for_scihub_first(monkeypatch, tmp_path):
    visited = []
    _patch_handlers(monkeypatch, visited)
    result = _get(tmp_path, "scihub_first", {"scihub_enabled": False})
    assert result["success"] is False
    assert "Unpaywall" in visited and "PublisherDirect" in visited
    assert set(visited).isdisjoint({"SciBban", "LibGen", "Sci-Hub"})
    assert _get(tmp_path, "scihub_only", {"scihub_enabled": False})["attempts"] == []


def test_oa_first_reaches_grey_after_all_legal_fail(monkeypatch, tmp_path):
    visited = []
    _patch_handlers(monkeypatch, visited, win="SciBban")
    result = _get(tmp_path, "oa_first", {"scihub_enabled": True})
    assert result["source"] == "SciBban"
    assert (
        visited.index("Unpaywall")
        < visited.index("PublisherDirect")
        < visited.index("SciBban")
    )
    assert "OpenAIRE" in visited and "CORE" in visited
    assert "Sci-Hub" not in visited


def test_configured_institution_channels_reachable_after_failed_legal(
    monkeypatch, tmp_path
):
    from academic_source import sources

    visited = []
    handler = _patch_handlers(monkeypatch, visited, win="SessionBroker")
    monkeypatch.setattr(sources, "_broker_source", handler("SessionBroker"))
    monkeypatch.setattr(
        sources, "_publisher_batch_source", handler("InstitutionalBrowser")
    )
    config = {
        "scihub_enabled": False,
        "carsi_enabled": True,
        "carsi_idp_name": "University",
        "vpnsci_enabled": True,
        "ezproxy_enabled": True,
        "elsevier_api_key": "test",
    }
    result = _get(tmp_path, "legal_only", config)
    assert result["source"] == "SessionBroker"
    assert visited[-4:] == ["CARSI", "WebVPN", "EZProxy", "SessionBroker"]
    assert "InstitutionalBrowser" not in visited


@pytest.mark.parametrize(
    "policy,key_source", [("fastest", "config"), ("legal_only", "env")]
)
def test_elsevier_api_short_circuits_other_sources(
    monkeypatch, tmp_path, policy, key_source
):
    from scansci_pdf.sources import publishers

    visited = []
    handler = _patch_handlers(monkeypatch, visited, win="ElsevierAPI")
    monkeypatch.setattr(
        publishers,
        "get_publisher_fast_sources",
        lambda doi: [
            (handler("Crossref"), "Crossref"),
            (handler("UnpaywallPublisher"), "Unpaywall"),
            (handler("ElsevierAPI"), "ElsevierAPI"),
            (handler("ElsevierBrowser"), "ElsevierBrowser"),
        ],
    )
    monkeypatch.delenv("ELSEVIER_API_KEY", raising=False)
    config = {"scihub_enabled": False}
    if key_source == "config":
        config["elsevier_api_key"] = "example-only"
    else:
        monkeypatch.setenv("ELSEVIER_API_KEY", "example-only")
    result = LegacySources().acquire(
        "10.1016/example",
        AcquisitionRequest(identifiers=["10.1016/example"], policy=policy),
        tmp_path,
        config,
    )
    assert result["source"] == "ElsevierAPI"
    assert visited == ["ElsevierAPI"]
    assert [attempt["source"] for attempt in result["attempts"]] == ["ElsevierAPI"]


@pytest.mark.parametrize(
    "doi,policy,has_key,first",
    [
        ("10.1016/example", "fastest", False, "Crossref"),
        ("10.1016/example", "legal_only", False, "Crossref"),
        ("10.1016/example", "oa_first", True, "Unpaywall"),
        ("10.1016/example", "scihub_first", True, "SciBban"),
        ("10.1016/example", "grey_only", True, "SciBban"),
        ("10.1016/example", "scihub_only", True, "Sci-Hub"),
        ("10.1038/example", "legal_only", True, "Crossref"),
    ],
)
def test_elsevier_shortcut_respects_key_policy_and_doi(
    monkeypatch, tmp_path, doi, policy, has_key, first
):
    from scansci_pdf.sources import publishers

    visited = []
    handler = _patch_handlers(monkeypatch, visited, win=first)
    monkeypatch.setattr(
        publishers,
        "get_publisher_fast_sources",
        lambda doi: [
            (handler("Crossref"), "Crossref"),
            (handler("UnpaywallPublisher"), "Unpaywall"),
            (handler("ElsevierAPI"), "ElsevierAPI"),
            (handler("ElsevierBrowser"), "ElsevierBrowser"),
        ],
    )
    monkeypatch.delenv("ELSEVIER_API_KEY", raising=False)
    result = LegacySources().acquire(
        doi,
        AcquisitionRequest(identifiers=[doi], policy=policy),
        tmp_path,
        {"scihub_enabled": True, "elsevier_api_key": "example-only" if has_key else ""},
    )
    assert result["source"] == first
    assert visited == [first]


def test_fastest_parallelizes_http_and_keeps_publisher_on_owner_thread(
    monkeypatch, tmp_path
):
    import threading

    from scansci_pdf.sources import openalex, publishers, semantic_scholar, unpaywall

    visited = []
    _patch_handlers(monkeypatch, visited)
    owner = threading.current_thread().name
    publisher_threads = []
    monkeypatch.setattr(
        publishers,
        "get_publisher_fast_sources",
        lambda doi: [
            (
                lambda *args: publisher_threads.append(threading.current_thread().name),
                "PublisherDirect",
            )
        ],
    )
    barrier = threading.Barrier(3)
    racing_threads = set()

    def http_handler(*args):
        racing_threads.add(threading.current_thread().name)
        barrier.wait(timeout=3)

    monkeypatch.setattr(unpaywall, "try_unpaywall", http_handler)
    monkeypatch.setattr(openalex, "try_openalex_oa", http_handler)
    monkeypatch.setattr(semantic_scholar, "try_semanticscholar", http_handler)
    _get(tmp_path, "fastest", {"scihub_enabled": False})
    assert publisher_threads == [owner]
    assert len(racing_threads) == 3


def test_webvpn_saved_session_headless_and_login_page_exits(monkeypatch, tmp_path):
    from scansci_pdf import browser_backend
    from scansci_pdf.sources import instsci

    class Browser:
        url = "https://example.org/article"
        heading = "Article"

        def new_context(self):
            return self

        def new_page(self):
            return self

        def on(self, *args):
            pass

        def goto(self, *args, **kwargs):
            pass

        def title(self):
            return self.heading

        def close(self):
            pass

    browser = Browser()
    launched = []
    monkeypatch.setattr(browser_backend, "is_available", lambda *args: True)
    monkeypatch.setattr(
        browser_backend, "launch", lambda **kw: launched.append(kw) or browser
    )
    monkeypatch.setattr(
        instsci, "_resolve_doi_url", lambda doi: "https://example.org/article"
    )
    monkeypatch.setattr(instsci, "convert_url", lambda url, base, config: url)
    monkeypatch.setattr(
        instsci,
        "_construct_publisher_pdf_url",
        lambda doi, url: "https://example.org/pdf",
    )
    sleeps = []
    monkeypatch.setattr(instsci.time, "sleep", lambda seconds: sleeps.append(seconds))

    def download(url, path, config, doi, context):
        _pdf(path)
        return {"success": True, "file": str(path), "source": "WebVPN"}

    monkeypatch.setattr(instsci, "_download_pdf_with_browser_cookies", download)
    config = {
        "vpnsci_enabled": True,
        "vpnsci_base_url": "https://vpn.example.org",
        "interactive": False,
        "cache_dir": str(tmp_path),
    }
    result = instsci._try_instsci_browser(
        "10.1234/paper", tmp_path / "paper.pdf", config
    )
    assert result["source"] == "WebVPN" and launched[-1]["headless"] is True
    browser.heading = "CAS 登录"
    sleeps.clear()
    assert (
        instsci._try_instsci_browser("10.1234/paper", tmp_path / "other.pdf", config)[
            "error_type"
        ]
        == "auth_required"
    )
    assert sleeps == [3]
    assert all(item["headless"] for item in launched)


def test_institutional_browser_login_gate_respects_saved_session(monkeypatch, tmp_path):
    from scansci_pdf.institutional.publisher_batch import PublisherBatchDownloader
    from scansci_pdf.sources.carsi import CARSIClient

    carsi = CARSIClient({"interactive": False, "cache_dir": str(tmp_path)})
    opened = []
    monkeypatch.setattr(
        carsi, "_browser_login", lambda publisher: opened.append(publisher) or False
    )
    monkeypatch.setattr(carsi, "_try_load_cookies", lambda publisher: False)
    assert carsi.login("Wiley") is False
    assert not opened
    monkeypatch.setattr(carsi, "_try_load_cookies", lambda publisher: True)
    assert carsi.login("Wiley") is True

    downloader = object.__new__(PublisherBatchDownloader)
    downloader.config = {"interactive": False}
    assert downloader._complete_login_from_current_page(None, None) is False


def test_readable_pdf_required_before_source_wins(monkeypatch, tmp_path):
    from scansci_pdf.sources import publishers

    def truncated(doi, path, config):
        path.write_bytes(b"%PDF-1.7\ntruncated")
        return {"success": True, "file": str(path)}

    def readable(doi, path, config):
        _pdf(path)
        return {"success": True, "file": str(path)}

    monkeypatch.setattr(
        publishers,
        "get_publisher_fast_sources",
        lambda doi: [(truncated, "Truncated"), (readable, "Readable")],
    )
    result = _get(tmp_path, "legal_only", {"scihub_enabled": False})
    assert result["source"] == "Readable"
    assert [item["source"] for item in result["attempts"]] == ["Truncated", "Readable"]
