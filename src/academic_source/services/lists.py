"""Normalize uploaded and inline paper lists into acquisition entries."""

import json
import re
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from academic_source.infrastructure.storage import Store

from .discovery import normalize_identifier

_TABLE_SUFFIXES = {".csv", ".tsv", ".tab", ".xlsx"}
_APA_AUTHOR = re.compile(r"[A-Z][a-z\u00C0-\u024F]+,\s+[A-Z]\.")
_BIBTEX_ENTRY = re.compile(r"^\s*@\w+\s*[{(]", re.MULTILINE)


def parse_list(
    store: Store, *, upload_id: str | None = None, text: str | None = None
) -> list[dict[str, Any]]:
    if (upload_id is None) == (text is None):
        raise ValueError("Provide exactly one of upload_id or text")
    if upload_id is not None:
        path = store.upload_path(upload_id)
        suffix = path.suffix.lower()
        if suffix in _TABLE_SUFFIXES:
            entries = _table_entries(path)
        else:
            try:
                content = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("Uploaded text must be UTF-8 encoded") from exc
            if suffix == ".json":
                entries = _json_entries(content)
            else:
                entries = _text_entries(content, bibtex=suffix == ".bib")
    else:
        assert text is not None
        entries = _text_entries(text)
    if not entries:
        raise ValueError("Paper list must contain at least one entry")
    return entries


def _text_entries(text: str, *, bibtex: bool = False) -> list[dict[str, Any]]:
    from scansci_pdf.paperlist import _parse_bib_to_entries, parse_apa_references
    from scansci_pdf.pipeline import extract_identifier, parse_queue

    if bibtex or _BIBTEX_ENTRY.search(text):
        papers = _parse_bib_to_entries(text)
    elif _APA_AUTHOR.search(text):
        papers = parse_apa_references(text)
    else:
        entries = []
        for entry in parse_queue(text):
            first = entry.raw.split("\t", 1)[0].strip()
            normalized = normalize_identifier(first)
            identifier = (
                normalized
                if normalized.startswith("arxiv:")
                else normalize_identifier(
                    entry.identifier or extract_identifier(first) or first
                )
            )
            if identifier:
                entries.append({"identifier": identifier, "raw": entry.raw})
        return entries
    return [
        {
            "identifier": normalize_identifier(paper.doi or paper.title or paper.raw),
            "title": paper.title,
            "doi": paper.doi,
            "raw": paper.raw,
        }
        for paper in papers
        if paper.doi or paper.title or paper.raw
    ]


def _table_entries(path: Path) -> list[dict[str, Any]]:
    from scansci_pdf.pipeline import entries_from_table, extract_identifier, read_table

    try:
        rows = read_table(path)
    except UnicodeDecodeError as exc:
        raise ValueError("Uploaded text must be UTF-8 encoded") from exc
    except BadZipFile as exc:
        raise ValueError("Invalid XLSX workbook") from exc
    if any(any(not isinstance(key, str) for key in row) for row in rows):
        raise ValueError("Table rows must match the header columns")
    queue = entries_from_table(rows)
    entries = []
    for row, item in zip(rows, queue):
        title = next(
            (
                str(value or "").strip()
                for key, value in row.items()
                if "title" in key.lower() or "标题" in key or "题名" in key
            ),
            "",
        )
        candidates = [
            str(value or "").strip()
            for key, value in row.items()
            if key.lower().strip() in {"identifier", "arxiv", "arxiv_id", "doi"}
        ]
        identifier = next(
            (found for value in candidates if (found := extract_identifier(value))),
            None,
        )
        if not identifier:
            identifier = (
                item.identifier
                or title
                or next(
                    (
                        str(value).strip()
                        for value in row.values()
                        if value and str(value).strip()
                    ),
                    "",
                )
            )
        if identifier:
            original = next(
                (
                    value
                    for value in candidates
                    if extract_identifier(value) == identifier
                ),
                identifier,
            )
            entries.append(
                {
                    "identifier": normalize_identifier(original),
                    "title": title,
                    "raw": item.raw or str(row)[:200],
                }
            )
    return entries


def _json_entries(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON paper list") from exc
    if not isinstance(payload, list):
        raise ValueError("JSON paper list must be an array")  # noqa: TRY004 - Invalid input is a client error.
    entries = []
    for item in payload:
        if isinstance(item, str):
            value = item
            entry: dict[str, Any] = {}
        elif isinstance(item, dict):
            entry = item.copy()
            value = item.get("identifier") or item.get("doi") or item.get("title")
        else:
            raise ValueError("JSON entries must be strings or objects")  # noqa: TRY004 - Invalid input is a client error.
        if not isinstance(value, str) or not value.strip():
            raise ValueError("JSON entry requires identifier, doi or title")
        entries.append({**entry, "identifier": normalize_identifier(value)})
    return entries
