"""Parse DOI, arXiv, and tabular paper queues without running download lanes."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .identifiers import OLD_ARXIV_RE, normalize_arxiv_id, normalize_doi

CHANNELS = ("oa", "elsevier", "grey", "institution", "auto")

# Preserve channel hints for existing paper-list formats.
CHANNEL_BY_PREFIX = {
    "10.1016": "elsevier",  # Elsevier / ScienceDirect / Cell / Lancet
    "10.3390": "oa",  # MDPI open access
}

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)


@dataclass
class QueueEntry:
    identifier: str = ""
    channel: str = "auto"
    oa_url: str = ""
    title: str = ""
    raw: str = ""
    unresolved: bool = False


def predict_channel(identifier: str) -> str:
    """Zero-cost channel prediction from the DOI prefix."""
    m = re.match(r"^(10\.\d{4,9})/", identifier.strip())
    if m:
        return CHANNEL_BY_PREFIX.get(m.group(1).lower(), "auto")
    return "auto"


def extract_identifier(text: str) -> str | None:
    """Extract a DOI or arXiv ID from a raw string, URL, or citation fragment."""
    raw = text.strip().rstrip(".,;)")
    if not raw:
        return None
    arxiv = normalize_arxiv_id(raw)
    if arxiv and (
        "arxiv" in raw.lower()
        or re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", raw)
        or OLD_ARXIV_RE.match(raw)
    ):
        # Bare old-style arXiv IDs need an explicit pattern match.
        return arxiv
    doi_m = DOI_RE.search(raw)
    if doi_m:
        doi = _clean_doi(doi_m.group(1))
        if doi:
            return doi
    return None


def _clean_doi(doi: str) -> str | None:
    doi = normalize_doi(doi)
    doi = doi.rstrip(".,;:)")
    # Trailing author glue, e.g. "10.1002/ird.2673Hamed" -> "10.1002/ird.2673"
    doi = re.sub(r"[A-Z][a-z\u00C0-\u024F]{1,}$", "", doi)
    return doi if re.match(r"^10\.\d{4,9}/\S", doi) else None


def parse_queue(text: str) -> list[QueueEntry]:
    """Parse queue/DOI-list text into entries; unresolvable lines are kept."""
    entries: list[QueueEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t")]
        ident = extract_identifier(parts[0])
        if not ident:
            entries.append(QueueEntry(raw=line, unresolved=True))
            continue
        channel = (
            parts[1].lower()
            if len(parts) > 1 and parts[1].lower() in CHANNELS
            else predict_channel(ident)
        )
        oa_url = (
            parts[2] if len(parts) > 2 and parts[2].lower().startswith("http") else ""
        )
        entries.append(
            QueueEntry(identifier=ident, channel=channel, oa_url=oa_url, raw=line)
        )
    return entries


def read_table(path: str | Path) -> list[dict[str, str]]:
    """Read a csv / tsv / xlsx table into rows of string dicts."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".csv", ".tsv", ".tab"):
        delim = "\t" if suffix in (".tsv", ".tab") else ","
        with open(p, newline="", encoding="utf-8-sig") as f:
            first = f.readline()
            rest = f.read()
        first_cells = [c.strip() for c in first.rstrip("\r\n").split(delim)]
        # An identifier in the first cell signals a headerless table.
        headerless = (
            bool(first_cells) and extract_identifier(first_cells[0]) is not None
        )
        if headerless:
            names = ["identifier", "channel", "oa_url"] + [
                f"col{i}" for i in range(len(first_cells) - 3)
            ]
            out = []
            for line in (first + rest).splitlines():
                if not line.strip():
                    continue
                cells = [c.strip() for c in line.rstrip("\r\n").split(delim)]
                out.append(
                    {
                        name: (cells[i] if i < len(cells) else "")
                        for i, name in enumerate(names)
                    }
                )
            return out
        import io as _io

        return [
            dict(r) for r in csv.DictReader(_io.StringIO(first + rest), delimiter=delim)
        ]
    if suffix == ".xlsx":
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("xlsx 支持需要 openpyxl：pip install openpyxl") from exc
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            header = [str(c or "").strip() for c in next(rows)]
        except StopIteration:
            return []
        out = []
        for r in rows:
            if all(c is None for c in r):
                continue
            out.append({h: ("" if c is None else str(c)) for h, c in zip(header, r)})
        return out
    raise ValueError(f"Unsupported table format: {suffix}")


def entries_from_table(rows: list[dict[str, str]]) -> list[QueueEntry]:
    """Map table rows to queue entries; the DOI column is sniffed if needed.

    Preserve channel and oa_url columns in headerless three-column queues.
    """
    if not rows:
        return []
    cols = list(rows[0].keys())
    doi_col = next((c for c in cols if re.search(r"\bdoi\b", c, re.IGNORECASE)), None)
    if doi_col is None:
        for c in cols:
            if any(DOI_RE.search(str(v)) for v in (r.get(c, "") for r in rows[:20])):
                doi_col = c
                break
    title_col = next(
        (c for c in cols if "title" in c.lower() or "标题" in c or "题名" in c), ""
    )
    channel_col = next(
        (c for c in cols if re.search(r"\bchannel\b", c, re.IGNORECASE)), None
    )
    oa_url_col = next(
        (c for c in cols if re.search(r"oa.?url", c, re.IGNORECASE)), None
    )
    entries: list[QueueEntry] = []
    for r in rows:
        raw_val = str(r.get(doi_col, "")).strip() if doi_col else ""
        ident = extract_identifier(raw_val) if raw_val else None
        if not ident:
            entries.append(QueueEntry(raw=str(r)[:200], unresolved=True))
            continue
        channel = (
            str(r.get(channel_col, "") or "").strip() if channel_col else ""
        ) or predict_channel(ident)
        oa_url = str(r.get(oa_url_col, "") or "").strip() if oa_url_col else ""
        entries.append(
            QueueEntry(
                identifier=ident,
                channel=channel,
                oa_url=oa_url,
                title=(str(r.get(title_col, "") or "") if title_col else ""),
            )
        )
    return entries

