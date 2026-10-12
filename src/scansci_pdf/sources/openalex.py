"""OpenAlex OA direct link source + Content API."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

import requests

from ..network import fetch, fetch_json, polite_delay
from ..pdf_utils import (
    _response_looks_pdf,
    download_pdf,
    is_pdf_file,
    is_plausible_pdf_url,
    success,
)
from ..upstream_client import openalex_content_url, openalex_params


def try_openalex_oa(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try OpenAlex best_oa_location for OA PDF link."""
    q = urllib.parse.quote(doi, safe="")
    params = openalex_params(
        config, {"mailto": config["email"]} if config.get("email") else {}
    )
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"https://api.openalex.org/works/doi:{q}{query}"
    try:
        payload = fetch_json(url, config)
        if not payload:
            return None
        oa = payload.get("open_access", {})
        if not oa.get("is_oa"):
            return None
        pdf_url = oa.get("oa_url") or ""
        if not pdf_url:
            primary = payload.get("primary_location", {})
            if isinstance(primary, dict):
                pdf_url = primary.get("pdf_url") or ""
        if pdf_url and is_plausible_pdf_url(pdf_url):
            polite_delay(config)
            result = download_pdf(pdf_url, output_path, config, "OpenAlexOA")
            if result:
                result["doi"] = doi
                result["identifier"] = doi
                return result
    except requests.RequestException:
        pass
    return None


def try_openalex_content_api(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try OpenAlex Content API for direct PDF download.

    Uses content.openalex.org which returns a 302 redirect to a signed
    Cloudflare R2 URL containing the actual PDF.
    Requires a free API key from https://openalex.org/users (set openalex_api_key in config).
    """
    api_key = config.get("openalex_api_key", "")
    if not api_key:
        return None

    q = urllib.parse.quote(doi, safe="")
    params = openalex_params(
        config, {"mailto": config["email"]} if config.get("email") else {}
    )
    meta_url = f"https://api.openalex.org/works/doi:{q}?{urllib.parse.urlencode(params)}"
    resp = None
    try:
        payload = fetch_json(meta_url, config)
        if not payload:
            return None

        work_id = payload.get("id", "")
        if not work_id:
            return None
        if "/" in work_id:
            work_id = work_id.rsplit("/", 1)[-1]

        oa = payload.get("open_access", {})
        if not oa.get("is_oa"):
            return None

        content_url = openalex_content_url(work_id, config)
        polite_delay(config)
        resp = fetch(content_url, config, stream=True)

        if resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if not _response_looks_pdf(resp, first):
            return None

        from .publishers import _write_pdf_atomic
        if not _write_pdf_atomic(output_path, first, iterator):
            return None

        if is_pdf_file(output_path):
            return success(doi, output_path, "OpenAlexContent")
    except requests.RequestException:
        pass
    finally:
        if resp is not None:
            resp.close()
    return None
