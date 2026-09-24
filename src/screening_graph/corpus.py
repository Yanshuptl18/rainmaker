"""Reading the shipped corpus.

This is done for you so the interesting work is elsewhere. `manifest.json` is the index; every
document in it has a `doc_id`, which is what answers must cite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "data" / "manifest.json"


@dataclass(frozen=True)
class Document:
    doc_id: str
    kind: str  # "filing" | "news"
    company_name: str | None
    subject: str | None
    announced_at: str | None  # as the exchange printed it, e.g. "17-Aug-2026 21:45:19"
    source_url: str | None
    raw: dict

    @property
    def title(self) -> str | None:
        return self.raw.get("title")

    def text(self) -> str:
        """The document's text: extracted PDF text for a filing, the headline for a news item."""
        rel = self.raw.get("text_file")
        if rel:
            return (REPO_ROOT / rel).read_text(encoding="utf-8")
        return self.raw.get("title") or ""

    def pdf_path(self) -> Path | None:
        rel = self.raw.get("pdf_file")
        return REPO_ROOT / rel if rel else None


@lru_cache(maxsize=1)
def load_manifest(path: Path = MANIFEST) -> tuple[Document, ...]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Document(
            doc_id=row["doc_id"],
            kind=row["kind"],
            company_name=row.get("company_name") or row.get("company_hint"),
            subject=row.get("subject"),
            announced_at=row.get("announced_at") or row.get("published"),
            source_url=row.get("source_url"),
            raw=row,
        )
        for row in rows
    )


def documents(kind: str | None = None) -> tuple[Document, ...]:
    docs = load_manifest()
    return docs if kind is None else tuple(d for d in docs if d.kind == kind)


def document(doc_id: str) -> Document:
    for doc in load_manifest():
        if doc.doc_id == doc_id:
            return doc
    raise KeyError(f"no such document: {doc_id}")
