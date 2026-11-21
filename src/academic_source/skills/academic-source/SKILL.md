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

`search` returns a JSON array. Each entry carries `source` (the backend that
found it), the bibliographic fields that backend has, and the full abstract when
one exists. Missing abstracts are filled by DOI from PubMed, so papers outside
biomedicine may still lack one. If a journal name is ambiguous, the error lists
candidates with ISSNs; retry with one of them. If a request times out upstream,
retry once, then report the failure.

Each call searches one backend, chosen with `--backend`:

| Backend | Use it for |
|---|---|
| `openalex` (default) | Topic searches, citation counts, open-access filters |
| `crossref` | Everything publishers have registered a DOI for, including papers posted today |
| `pubmed` | Biomedicine; also finds papers that have no DOI or that Crossref lacks |

All backends accept `--journal` (ISSN or exact name), `--year`, `--author`,
`--type` and `--sort publication_date`. Write `--type` in the backend's own
vocabulary: `article` (OpenAlex), `journal-article` (Crossref),
`'Journal Article'` (PubMed). `--oa`, `--min-citations`, `--in` and
`--sort cited_by_count` work only on OpenAlex. Only OpenAlex leaves out non-paper
items and retractions by default; with the other two, screen titles and types
yourself.

`--filter` is passed to the selected backend unchanged, so its syntax depends on
the backend. The sections below give forms that have been tested.

`--limit` accepts up to 1000. Write large results to a file rather than reading
them inline. To list a journal's papers, use one wide date window instead of
many narrow ones.

### OpenAlex

```bash
asource search 'CRISPR' --filter 'authorships.countries:cn,language:en'
asource search --journal 0092-8674 --limit 1000 \
  --filter 'from_publication_date:2026-07-01,to_publication_date:2026-11-30' > cell.json
```

In `--filter`, a comma means AND, `|` means OR within one field, `!` negates.
Field list: https://docs.openalex.org/api-entities/works/filter-works

### Crossref

```bash
asource search --backend crossref --journal 0028-0836 --limit 1000 \
  --filter 'from-online-pub-date:2026-10-19,until-online-pub-date:2026-11-06' > nature.json
asource search --backend crossref --filter 'issn:0028-0836,issn:0036-8075,from-online-pub-date:2026-11-19'
asource search --backend crossref 'residual learning' --author 'Kaiming He'
```

- `--filter` is `name:value` pairs separated by commas. Different names
  combine with AND; a repeated name means OR (the second example lists both
  journals).
- Dates take `YYYY`, `YYYY-MM` or `YYYY-MM-DD`, and both ends are included. An
  upper bound of `2025-12` includes all of December.
- Choose the date field by question:
  - `from-online-pub-date` / `until-online-pub-date`: the publisher's online
    date. Records without an online date never match; there is no fallback to
    the print date.
  - `from-pub-date` / `until-pub-date`: the earliest publication date (what
    `--year` uses).
  - `from-created-date` / `until-created-date`: when Crossref first received the
    record.
- Other useful filters: `type:journal-article`, `prefix:10.1038`,
  `has-abstract:true`. Type names: https://api.crossref.org/types
- An unknown filter name or type fails with an error that lists the valid
  values.
- `journal-article` also covers news and editorials (Nature news DOIs start
  `10.1038/d41586`).
- Keyword ranking is loose: an exact title can rank second behind a loosely
  related item. Check titles; for a known DOI use `asource metadata --doi`.
  `--author` is a text match on author names, not an identity.

### PubMed

```bash
asource search --backend pubmed --journal 0036-8075 --limit 1000 \
  --filter '2026/11/01:2026/11/20[epdat]' > science.json
asource search --backend pubmed \
  'diabetes[tiab] AND (exercise[tiab] OR walking[tiab]) NOT Review[pt]'
asource search --backend pubmed 'doudna ja[au] AND 2025[dp] NOT preprint[pt]'
```

- The query and `--filter` are PubMed search expressions, joined with AND. Use
  uppercase `AND`, `OR`, `NOT` and parentheses.
- Put `NOT` in the query, not in `--filter`. `--filter 'NOT preprint[pt]'`
  becomes `... AND (NOT preprint[pt])`, which PubMed rejects.
- Useful tags: `[tiab]` title or abstract, `[ti]` title, `[au]` author as
  `doudna ja`, `[1au]` first author, `[mh]` MeSH term, `[pt]` publication type,
  `[issn]` journal ISSN, `[doi]` DOI. Use `[issn]` rather than `[ta]`:
  `Nature[ta]` also matches other journals.
- Words without a tag are expanded through PubMed's term mapping (synonyms,
  MeSH headings). `diabetes` finds about 40% more records than
  `diabetes[tiab]`. Tag the words when you want them literally.
- Dates are `YYYY/MM/DD`, `YYYY/MM` or `YYYY`; a range is `start:end`. Pick the
  tag by question: `[epdat]` online publication (use it for recently posted
  papers), `[dp]` publication date (what `--year` uses), `[crdt]` when PubMed
  created the record.
- Mistakes that fail with an error showing PubMed's `querytranslation`:
  unbalanced parentheses, a dangling `AND`, a quoted phrase PubMed cannot find.
  A misspelled tag does not fail: `lamin[xyz]` silently becomes `lamin`. If
  results look too broad, check the tag spelling.
- PubMed also indexes bioRxiv and medRxiv preprints; add `NOT preprint[pt]` to
  leave them out.
- Results come newest first; `:asc` is not supported. With keywords or
  `--author`, results come in PubMed's own order.
- Every result has `pmid`; some have no `doi`.

### OpenAlex data quirks

These matter when screening by journal and date:

- For some journals (Cell, for example) `publication_date` is only the month:
  every paper is dated the 1st. A window starting on the 2nd of a month misses
  that month entirely. Use windows aligned to whole months and check exact
  online dates elsewhere if they matter.
- `publication_date` is usually the first online date, which can be weeks
  earlier than the date PubMed gives.
- News, editorials and comments are often typed `article`. For Nature, research
  papers have DOIs starting `10.1038/s41586`; `10.1038/d41586` is news.
- Some research papers are typed `review`, so `--type article` drops them.
  Leave `--type` out when listing a journal and screen titles instead.
- The list can be incomplete. Papers posted in the last day or two may not be
  indexed yet, and a few papers are missing for weeks or carry no journal, so
  a journal filter cannot find them. When the list must be complete, run a
  separate search with `--backend crossref` or `--backend pubmed` and keep track
  of which backend found each paper.

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
