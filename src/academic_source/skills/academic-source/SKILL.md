---
name: academic-source
description: Search scholarly papers, look up DOI or arXiv metadata, and download full text (PDF and Markdown) with the asource command-line client. Use whenever a task needs to find papers, read a paper's full text, or fetch papers in bulk.
---

# academic-source: find and fetch papers with `asource`

`asource` talks to an academic-source server. Results arrive as one JSON document
on stdout; progress goes to stderr.

## Before the first command

Two environment variables must be set:

- `ACADEMIC_SOURCE_SERVER`: the server URL.
- `ACADEMIC_SOURCE_API_KEY_FILE`: a file holding the server key.

If `asource search test --limit 1` fails with "No server given" or HTTP 401, ask
the user for the missing value. Do not look for keys yourself.

## Find papers

```bash
asource search 'prime editing efficiency' --limit 10
asource search 'base editing' --journal 'Nature Biotechnology' --year 2022- --sort cited_by_count
asource metadata --doi 10.1126/science.aad8559
asource metadata --arxiv 2401.01234
```

`search` returns a JSON array; each entry has `doi`, `title`, `authors`, `year`,
`publication_date`, `journal`, `cited_by_count`, `is_oa`, `type` and the full
`abstract` (from OpenAlex, or PubMed when OpenAlex has none; papers outside
biomedicine may still lack one). `metadata` also returns the abstract.
Narrow results with
`--journal` (ISSN or exact name), `--year`, `--type`, `--oa`,
`--min-citations`, `--author` and `--in title`; anything else goes into
`--filter` as an OpenAlex filter expression. Run `asource search --help` for the
syntax and examples. If a journal name is ambiguous, the error lists candidates
with ISSNs; retry with the right ISSN. If `search` fails with an upstream error
such as HTTP 504, retry once, then report the failure.

`--limit` accepts up to 1000; above 100 the server fetches several pages, so
write large results to a file (`> results.json`) rather than reading them inline.
To list everything a journal published in a period, use one wide date window
instead of many narrow ones:

```bash
asource search --journal 0092-8674 --type article --limit 1000 \
  --filter from_publication_date:2026-07-01,to_publication_date:2026-11-30 > cell.json
```

Two quirks of OpenAlex data matter when screening by journal and date:

- For some journals (Cell, for example) `publication_date` is only the month:
  every paper is dated the 1st. A window starting on the 2nd of a month misses
  that month entirely. Use windows aligned to whole months and check exact
  online dates elsewhere if they matter.
- News, editorials and comments are often typed `article`. For Nature, research
  papers have DOIs starting `10.1038/s41586`; `10.1038/d41586` is news.

## Fetch full text

1. Collect every DOI you need first, then write them to a file, one per line.
   Only DOIs and arXiv IDs (`arxiv:2401.01234`) are accepted. If you have a
   title or a URL, find its DOI with `search` first.
2. Start **one** `batch` for the whole list, as a background command:

```bash
asource batch refs.txt --markdown --output papers > papers/job.json 2> papers/progress.log
```

Create the output directory before running it. The command submits the job,
waits for it, downloads the files, and exits; there is nothing to poll. The
server handles one paper at a time, often a minute or more each, so a
foreground command would time out. Continue with other work and read
`papers/job.json` when the background command finishes. `papers/progress.log`
shows queue position and one line per finished paper if you want to check on it.

Use `asource get DOI --markdown --output papers` only when you need exactly one
paper. Several small jobs queue behind each other and take longer than one job
with the same papers.

Exit code 0 means every paper succeeded. Exit code 1 with a JSON document on
stdout means some papers failed; the rest are saved and usable.

## Fetch supplementary material

Use `asource supp DOI --output papers` to fetch just the supplementary files.
Pass a file with one DOI per line instead of a single DOI for a batch. Use
`--supplementary` with `get` or `batch` to fetch both the main PDF and its
supplements. Add `--markdown` to convert supplementary PDFs too.

Each supplementary file carries a `note` with its title and caption when
available. Files taken from PMC are marked with their PMCID; they may differ
from the publisher's version. Check `warnings` for missing files.

A main PDF that is only a preprint carries a warning saying so; tell the user
when you rely on such a PDF, since it is not the peer-reviewed version.

## Read the results

Read the Markdown files the command saved. They are converted on the server; do
not extract text from the PDFs yourself. List them with:

```bash
jq -r '.results[] | select(.status=="succeeded") | .identifier as $d | .artifacts[] | select(.kind=="markdown") | "\($d)\t\(.path)"' papers/job.json
```

More ready-made queries and the field reference are in
[references/schema.md](references/schema.md).

## When a paper fails

List failures with their reasons:

```bash
jq -r '.results[] | select(.status=="failed") | [.identifier, .reason, .message // ""] | @tsv' papers/job.json
```

| `reason` | What to do |
|---|---|
| `rate_limited` | Retry that paper once after `rate_limit.retry_at`. |
| `unsupported` | The identifier was not a DOI or arXiv ID. Find the DOI and submit again. |
| anything else | Report the paper as unavailable, with its reason. Do not resubmit it or change `--policy`. |

State in your report which papers you could not read.

## Stop or resume

- Stopping the background command cancels the job on the server. The paper in
  progress finishes, the rest are skipped, and finished files are kept.
- `asource cancel JOB_ID` does the same from another shell.
- `asource download JOB_ID --output papers` saves the finished files of an
  earlier job, for example after a cancellation. The job id is the `id` field of
  the JSON document and appears on every progress line.
