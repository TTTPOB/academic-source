# `asource` output reference

Every command prints one JSON document on stdout. This file lists the fields an
agent needs and gives a query for each common question.

## Job document (`get`, `batch`, `status`, `download`, `cancel`)

```text
id            job id
status        queued | running | succeeded | partial | failed | cancelled | interrupted
total         number of papers in the job
completed     number of papers finished so far
results[]     one entry per finished paper, in submission order
artifacts[]   every file of the job, same entries as results[].artifacts[]
```

`partial` means some papers succeeded and some failed. `cancelled` and
`interrupted` jobs keep the results finished before they stopped.

### `results[]`

```text
identifier        the DOI or arXiv ID as submitted
status            succeeded | failed
cached            true when the server already had the paper      (succeeded only)
elapsed_seconds   time spent on this paper
warnings[]        conversion warnings, for example unreadable symbols
artifacts[]       files for this paper                             (succeeded only)
reason            short failure code                               (failed only)
message, action   present for failures the caller can act on
rate_limit        {source, retry_at} when reason is rate_limited
```

Common `reason` values: `network_error`, `rate_limited`, `auth_required`,
`not_found`, `unsupported`, `invalid_document`, `config_needed`,
`internal_error`.

### `artifacts[]`

```text
kind                pdf | markdown | supplementary | bibtex
identifier          the paper this file belongs to
filename            server-side name, for example oa-1.pdf
path                local file, present after get, batch or download saved it
size                bytes
provenance.source   where the PDF came from, for example Unpaywall
provenance.url      the URL it was fetched from
```

Local files are named `<artifact id>-<filename>`. Use `path`; do not build the
name yourself.

## Ready-made queries

Job outcome in one line:

```bash
jq -r '"\(.status) \(.completed)/\(.total)"' job.json
```

Markdown path for each paper:

```bash
jq -r '.results[] | select(.status=="succeeded") | .identifier as $d | .artifacts[] | select(.kind=="markdown") | "\($d)\t\(.path)"' job.json
```

Failed papers with reasons:

```bash
jq -r '.results[] | select(.status=="failed") | [.identifier, .reason, .message // ""] | @tsv' job.json
```

Papers to retry later, with the earliest retry time:

```bash
jq -r '.results[] | select(.reason=="rate_limited") | "\(.identifier)\t\(.rate_limit.retry_at)"' job.json
```

Where each PDF came from:

```bash
jq -r '.artifacts[] | select(.kind=="pdf") | "\(.identifier)\t\(.provenance.source)"' job.json
```

The same in Python:

```python
import json

job = json.load(open("job.json"))
markdown = {
    result["identifier"]: artifact["path"]
    for result in job["results"]
    for artifact in result.get("artifacts", [])
    if artifact["kind"] == "markdown"
}
failed = {
    r["identifier"]: r["reason"] for r in job["results"] if r["status"] == "failed"
}
```

## `search`

A JSON array, best match first:

```text
doi, title, authors[], year, publication_date, type
cited_by_count, is_oa, oa_url, is_retracted
```

```bash
asource search 'yeast replicative lifespan' --limit 20 | jq -r '.[] | [.doi, .year, .cited_by_count, .title] | @tsv'
```

Entries without a DOI cannot be fetched.

## `metadata`

```text
id_type           doi | arxiv
doi               the identifier looked up
metadata_status   found, or a failure status
title, authors[], year, type
```
