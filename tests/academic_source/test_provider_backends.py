"""Regression checks for the provider transport inventory (no live I/O)."""

from academic_source.sources.registry import PROVIDERS


def test_non_browser_backends():
    expected = dict.fromkeys(
        [
            "Unpaywall",
            "OpenAlexOA",
            "OpenAlexContent",
            "EuropePMC",
            "CORE",
            "PMC",
            "Crossref",
            "NatureDirect",
            "arXiv",
            "SciBban",
        ],
        "http",
    )
    expected.update(
        dict.fromkeys(
            [
                "OpenAIRE",
                "DOAJ",
                "CrossrefPage",
                "PublisherDirect",
                "MDPIDirect",
                "ElsevierAPI",
                "PNASDirect",
                "SpringerTDM",
                "PLOSDirect",
                "FrontiersDirect",
                "BMCDirect",
                "SAGECN",
            ],
            "http_ungated",
        )
    )
    assert {
        label: provider.backend
        for label, provider in PROVIDERS.items()
        if provider.backend != "browser"
    } == expected


def test_browser_backends_include_camofox_and_http_fallbacks():
    for label in ("CopernicusDirect", "ScienceBrowser", "LibGen"):
        assert PROVIDERS[label].backend == "browser"


def test_oa_browser_fallback_requires_cdp():
    provider = PROVIDERS["OACDP"]
    assert provider.backend == "browser"
    assert provider.is_enabled("10.1234/example", {"browser_backend": "cdp"})
    assert not provider.is_enabled("10.1234/example", {"browser_backend": "patchright"})
