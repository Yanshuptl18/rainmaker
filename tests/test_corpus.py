"""The corpus loads and every file it names exists. This passes before you write anything."""

from screening_graph import corpus


def test_the_manifest_lists_both_kinds_of_document():
    kinds = {doc.kind for doc in corpus.documents()}
    assert kinds == {"filing", "news"}


def test_every_filing_has_text_and_a_pdf_on_disk():
    for doc in corpus.documents("filing"):
        assert doc.text().strip(), f"{doc.doc_id} has no text"
        assert doc.pdf_path().exists(), f"{doc.doc_id} has no pdf"


def test_every_document_carries_a_source_url():
    for doc in corpus.documents():
        assert doc.source_url, f"{doc.doc_id} has no source url"
