"""Golden label order for the legacy DOI fallback planner (no provider I/O)."""

import pytest

from academic_source.domain import AcquisitionRequest
from academic_source.sources import _plan

FAST_OA = "OpenAIRE DOAJ CrossrefPage CORE"
LEGACY_OA_TAIL = "OpenAlexOA OpenAIRE DOAJ CrossrefPage EuropePMC CORE PMC"
GREY = "SciBban LibGen Sci-Hub"
INSTITUTIONAL_TAIL = "SessionBroker InstitutionalBrowser"

# The fragments are snapshots, not data read from publisher routing at test time.
PUBLISHER_CASES = [
    ("10.1002 10.1111", "WileyBrowser Crossref"),
    ("10.1007 10.1023", "SpringerTDM SpringerBrowser Crossref"),
    ("10.1021", "ACSBrowser Crossref"),
    ("10.1038", "NatureDirect PublisherDirect NatureBrowser Crossref"),
    ("10.1039", "RSCBrowser Crossref"),
    ("10.1061", "ASCEBrowser Crossref"),
    ("10.1063 10.1116", "AIPBrowser Crossref"),
    ("10.1073", "PNASDirect Crossref"),
    ("10.1080", "TandFBrowser Crossref"),
    ("10.1088 10.1143", "IOPBrowser Crossref"),
    ("10.1093", "OxfordBrowser Crossref"),
    ("10.1098", "RoyalSocietyBrowser Crossref"),
    ("10.1103", "APSBrowser Crossref"),
    ("10.1109", "IEEEBrowser Crossref"),
    ("10.1126", "ScienceDirect ScienceBrowser Crossref"),
    ("10.1145", "ACMBrowser Crossref"),
    ("10.1177", "SAGEBrowser Crossref"),
    ("10.1186", "BMCDirect Crossref"),
    ("10.1371", "PLOSDirect Crossref"),
    ("10.3389", "FrontiersDirect Crossref"),
    ("10.3390", "MDPIDirect Crossref"),
    (
        "10.1146 10.1364 10.15252 10.20944 10.21203 10.26434 10.31219 10.3762 10.3938",
        "Crossref",
    ),
    ("10.1101 10.48550 10.9999", ""),
]
ELSEVIER_LEGACY = "Crossref Unpaywall ElsevierAPI ElsevierBrowser"
ELSEVIER_DEFAULT = "Crossref ElsevierAPI ElsevierBrowser"
ELSEVIER_WITH_KEY = "ElsevierAPI Crossref ElsevierBrowser"


@pytest.fixture(autouse=True)
def _clear_elsevier_key(monkeypatch):
    monkeypatch.delenv("ELSEVIER_API_KEY", raising=False)


def _labels(doi, policy, config):
    request = AcquisitionRequest(identifiers=[doi], policy=policy)
    return [label for label, _ in _plan(doi, request, config)]


@pytest.mark.parametrize(
    "doi,fast,legacy",
    [
        pytest.param(f"{prefix}/example", labels, f"{labels} Unpaywall", id=prefix)
        for prefixes, labels in PUBLISHER_CASES
        for prefix in prefixes.split()
    ]
    + [
        pytest.param(
            "10.1016/example", ELSEVIER_DEFAULT, ELSEVIER_LEGACY, id="10.1016"
        ),
        pytest.param(
            "10.1016/j.cell.example",
            ELSEVIER_DEFAULT,
            ELSEVIER_LEGACY,
            id="10.1016-j.cell",
        ),
    ],
)
@pytest.mark.parametrize(
    "policy,expected",
    [
        ("fastest", f"{{fast}} {FAST_OA} {GREY}"),
        ("oa_first", f"{FAST_OA} {{fast}} {GREY}"),
        ("legal_only", f"{{fast}} {FAST_OA}"),
        ("scihub_first", f"{GREY} {{legacy}} {LEGACY_OA_TAIL}"),
        ("grey_only", GREY),
        ("scihub_only", "Sci-Hub"),
    ],
)
def test_default_plan(doi, fast, legacy, policy, expected):
    assert _labels(doi, policy, {}) == expected.format(fast=fast, legacy=legacy).split()


@pytest.mark.parametrize(
    "doi,publisher,shortcut,legacy,institutional",
    [
        (
            "10.1016/example",
            ELSEVIER_DEFAULT,
            ELSEVIER_WITH_KEY,
            ELSEVIER_LEGACY,
            f"CARSI WebVPN EZProxy {INSTITUTIONAL_TAIL}",
        ),
        (
            "10.1177/example",
            "SAGEBrowser Crossref",
            "SAGEBrowser Crossref",
            "SAGEBrowser Crossref Unpaywall",
            f"SAGECN CARSI WebVPN EZProxy {INSTITUTIONAL_TAIL}",
        ),
    ],
)
@pytest.mark.parametrize(
    "policy,expected",
    [
        ("fastest", f"{{shortcut}} {FAST_OA} {{institutional}}"),
        ("oa_first", f"{FAST_OA} {{publisher}} {{institutional}}"),
        ("legal_only", f"{{shortcut}} {FAST_OA} {{institutional}}"),
        (
            "scihub_first",
            f"{{legacy}} {LEGACY_OA_TAIL} OpenAlexContent {{institutional}}",
        ),
        ("grey_only", ""),
        ("scihub_only", ""),
    ],
)
def test_configured_plan(
    doi, publisher, shortcut, legacy, institutional, policy, expected
):
    config = {
        "elsevier_api_key": "example-only",
        "openalex_api_key": "example-only",
        "scihub_enabled": False,
        "carsi_enabled": True,
        "carsi_idp_name": "University",
        "vpnsci_enabled": True,
        "ezproxy_enabled": True,
    }
    assert (
        _labels(doi, policy, config)
        == expected.format(
            publisher=publisher,
            shortcut=shortcut,
            legacy=legacy,
            institutional=institutional,
        ).split()
    )


@pytest.mark.parametrize("policy", ["fastest", "legal_only"])
def test_elsevier_environment_key_changes_priority(monkeypatch, policy):
    monkeypatch.setenv("ELSEVIER_API_KEY", "example-only")
    expected = f"{ELSEVIER_WITH_KEY} {FAST_OA}"
    assert (
        _labels("10.1016/example", policy, {"scihub_enabled": False})
        == expected.split()
    )


@pytest.mark.parametrize(
    "config,institutional",
    [
        ({"carsi_enabled": True}, ""),
        ({"carsi_idp_name": "University"}, ""),
        (
            {"carsi_enabled": True, "carsi_idp_name": "University"},
            f"SAGECN CARSI {INSTITUTIONAL_TAIL}",
        ),
        ({"vpnsci_enabled": True}, f"WebVPN {INSTITUTIONAL_TAIL}"),
        ({"ezproxy_enabled": True}, f"EZProxy {INSTITUTIONAL_TAIL}"),
    ],
)
def test_individual_institutional_switches(config, institutional):
    expected = f"SAGEBrowser Crossref {FAST_OA} {GREY} {institutional}"
    assert _labels("10.1177/example", "fastest", config) == expected.split()
