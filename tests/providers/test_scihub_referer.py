"""Sci-Hub PDF CDN requires a same-site Referer (sci.bban.top 403s without)."""

from __future__ import annotations

from pathlib import Path

from scansci_pdf.sources import scihub


def test_referer_threaded_to_download(monkeypatch, tmp_path: Path):
    """try_scihub_domain passes the mirror landing URL as Referer."""
    captured: dict = {}

    class _Page:
        status_code = 200
        url = "https://sci-hub.vg/10.1/x"
        _content = b""  # no buffered body; the flow falls back to resp.raw

        def __init__(self):
            self.headers = {"content-type": "text/html"}
            self.cookies = {"session": "x"}
            self._iter = iter(
                [
                    b'<html><meta name="citation_pdf_url" content="https://sci.bban.top/pdf/10.1/x.pdf"></html>'
                ]
            )

        def iter_content(self, chunk_size=8192):
            return self._iter

    monkeypatch.setattr(scihub, "fetch", lambda *a, **k: _Page())
    monkeypatch.setattr(
        "scansci_pdf.pdf_utils.download_pdf",
        lambda url, out, cfg, src, **kw: captured.update(kw) or None,
    )

    scihub.try_scihub_domain(
        "10.1/x",
        "https://sci-hub.vg",
        tmp_path / "o.pdf",
        {"scihub_browser_first": False},
    )

    assert captured.get("referer") == "https://sci-hub.vg/10.1/x"
