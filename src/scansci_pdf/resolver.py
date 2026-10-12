"""Title-to-DOI resolver via OpenAlex search with similarity matching."""

from __future__ import annotations

import time
from typing import Any

from .paperlist import PaperEntry


def resolve_title_to_doi(title: str, config: dict[str, Any]) -> str | None:
    """Search OpenAlex by title and return best-matching DOI."""
    from .search import search_by_title

    match = search_by_title(title, config)
    return match["doi"] if match else None


def batch_resolve(
    entries: list[PaperEntry],
    config: dict[str, Any],
    delay: float = 0.3,
) -> dict[str, Any]:
    """Batch resolve: fix DOI format + search by title for missing DOIs.

    Returns dict with 'entries' (resolved list) and 'stats'.
    """
    total = len(entries)
    resolved_by_title = 0
    already_has_doi = 0
    unresolvable = 0

    for i, entry in enumerate(entries):
        if entry.doi:
            already_has_doi += 1
            continue

        if not entry.title:
            unresolvable += 1
            continue

        # Search by title
        doi = resolve_title_to_doi(entry.title, config)
        if doi:
            entry.doi = doi
            resolved_by_title += 1
        else:
            unresolvable += 1

        # Rate limit for OpenAlex API
        if delay > 0 and i < total - 1:
            time.sleep(delay)

    return {
        "entries": entries,
        "stats": {
            "total": total,
            "already_has_doi": already_has_doi,
            "resolved_by_title": resolved_by_title,
            "unresolvable": unresolvable,
        },
    }
