"""Check paper-list transport parity and malformed-input boundaries."""

from io import BytesIO

import pytest

from academic_source.infrastructure.storage import Store
from academic_source.services.discovery import normalize_identifier, resolve, search
from academic_source.services.lists import parse_list
from academic_source.settings import Settings


def test_inline_bibtex_and_uploaded_bibtex_have_the_same_papers(tmp_path):
    store = Store(Settings(data_dir=tmp_path))
    bib = """@article{first,
  title = {A Proper Study},
  doi = {10.1234/first},
}
@article{second,
  title = {The Untitled DOI Case},
}
"""
    uploaded = store.put_upload("papers.bib", BytesIO(b"\xef\xbb\xbf" + bib.encode()))
    inline = parse_list(store, text=bib)
    assert parse_list(store, upload_id=uploaded.id) == inline
    assert [entry["identifier"] for entry in inline] == [
        "10.1234/first",
        "The Untitled DOI Case",
    ]


def test_table_arxiv_and_title_rows_survive_doi_sniffing(tmp_path):
    store = Store(Settings(data_dir=tmp_path))
    data = (
        "doi,identifier,title\n"
        "10.1234/known,,Known\n"
        ",arxiv:hep-th/9901001v3,Old Arxiv Paper\n"
        ",,Title Waiting For Resolution\n"
    )
    upload = store.put_upload("papers.csv", BytesIO(data.encode()))
    entries = parse_list(store, upload_id=upload.id)
    assert [entry["identifier"] for entry in entries] == [
        "10.1234/known",
        "arxiv:hep-th/9901001v3",
        "Title Waiting For Resolution",
    ]


def test_plain_text_channels_and_arxiv_urls_keep_unresolved_titles(tmp_path):
    store = Store(Settings(data_dir=tmp_path))
    text = (
        "https://arxiv.org/abs/2301.12345v2\n"
        "A Paper Without Known DOI\n"
        "https://doi.org/10.1234/known\toa\n"
    )
    upload = store.put_upload("list.txt", BytesIO(b"\xef\xbb\xbf" + text.encode()))
    expected = ["arxiv:2301.12345v2", "A Paper Without Known DOI", "10.1234/known"]
    assert [entry["identifier"] for entry in parse_list(store, text=text)] == expected
    assert [
        entry["identifier"] for entry in parse_list(store, upload_id=upload.id)
    ] == expected


def test_empty_and_malformed_uploads_fail_before_job_submission(tmp_path):
    store = Store(Settings(data_dir=tmp_path))
    valid = store.put_upload(
        "valid.json", BytesIO(b'["doi:10.1234/one", {"title": "Without DOI"}]')
    )
    assert [entry["identifier"] for entry in parse_list(store, upload_id=valid.id)] == [
        "10.1234/one",
        "Without DOI",
    ]
    for filename, data in (
        ("empty.txt", b" \n# comment\n"),
        ("bad.json", b'[{"title": "valid"}, 42]'),
        ("invalid.json", b"[bad"),
        ("binary.txt", b"\xff"),
    ):
        upload = store.put_upload(filename, BytesIO(data))
        with pytest.raises(ValueError):
            parse_list(store, upload_id=upload.id)
    with pytest.raises(ValueError):
        parse_list(store, text=" \n")
    with pytest.raises(ValueError):
        parse_list(store, upload_id="x", text="10.1234/known")


def test_discovery_validates_before_search_and_keeps_public_abstract(monkeypatch):
    from scansci_pdf import search as legacy_search

    def fixture_search(query, limit):
        assert (query, limit) == ("papers", 2)
        return [
            {
                "title": "Paper",
                "abstract": "Useful",
                "url": "https://example.org/work",
                "file_path": "/private/paper.pdf",
            }
        ]

    monkeypatch.setattr(legacy_search, "search_papers", fixture_search)
    assert search(" papers ", limit=2) == [
        {"title": "Paper", "abstract": "Useful", "url": "https://example.org/work"}
    ]
    for query, limit in (("", 1), ("papers", 0), ("papers", 101)):
        with pytest.raises(ValueError):
            search(query, limit)
    assert (
        normalize_identifier("https://arxiv.org/pdf/math-ph/9901001v4.pdf")
        == "arxiv:math-ph/9901001v4"
    )
    assert resolve("doi: 10.1234/known", {})["identifier"] == "10.1234/known"
