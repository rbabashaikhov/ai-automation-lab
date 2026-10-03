"""Orchestrates read products/specs -> build document+chunks -> (optionally) persist.

## Change detection

`source_hash` (Phase 2, on `products`) already tells ingestion whether a
product's *scraped* content changed. This module adds the next link:
`documents.content_hash` is a SHA-256 over the *built document text*,
which is a deterministic function of the same typed columns +
`product_specs` that `source_hash` covers -- so in practice an unchanged
`source_hash` implies an unchanged `content_hash`, and a changed
`source_hash` is the trigger to rebuild and recompare. This module always
rebuilds and recomputes `content_hash` (rebuilding is cheap: pure string
formatting, no network/API calls) and only *reports* whether it actually
changed from what's currently stored -- it does not read `source_hash` as
a shortcut to skip rebuilding, since that would need to assume
`source_hash`'s exact field set stays in permanent lockstep with the
document text (see the Phase 3A ADR for why this module treats that as
merely a strong correlation, not a substitute to skip on). Each chunk's
own `content_hash` covers just that chunk's own text, so
`repository.py`'s `sync_chunks` (Phase 3B.1) can tell *which specific
chunks* actually changed, not just "the document changed somewhere" --
e.g. a price update only changes the `overview` chunk's hash, and the
`gaming`/`audio`/... chunks keep their existing embedding untouched. See
`repository.py` module docstring for the persistence-side algorithm this
enables.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .builder import DocumentDraft, build_document
from .chunker import ChunkDraft, build_chunks
from .models import Product, Spec
from .repository import ChunkSyncResult, IndexingRepository, ProductReader


@dataclass
class IndexingOutcome:
    product_id: int
    model_code: str | None
    external_id: str
    document_type: str
    content_hash: str
    previous_content_hash: str | None
    document_changed: bool
    chunk_count: int
    chunk_sections: list[str]
    persisted: bool
    document_inserted: bool | None = None
    chunk_sync: ChunkSyncResult | None = None


def build_for_product(product: Product, specs: list[Spec]) -> tuple[DocumentDraft, list[ChunkDraft]]:
    document = build_document(product, specs)
    chunks = build_chunks(product, specs)
    return document, chunks


def index_product(
    reader: ProductReader,
    product_id: int,
    *,
    dry_run: bool,
    repository: IndexingRepository | None = None,
) -> IndexingOutcome:
    product = reader.get_product(product_id)
    if product is None:
        raise ValueError(f"no product with id={product_id}")
    specs = reader.get_specs(product_id)

    document, chunks = build_for_product(product, specs)
    previous_hash = reader.get_existing_document_hash(product_id, document.document_type)
    changed = previous_hash != document.content_hash

    document_inserted: bool | None = None
    chunk_sync: ChunkSyncResult | None = None
    persisted = False
    if not dry_run:
        assert repository is not None
        result = repository.save_document_with_chunks(product_id, document, chunks)
        document_inserted = result.inserted
        chunk_sync = result.chunk_sync
        persisted = True

    return IndexingOutcome(
        product_id=product.id,
        model_code=product.model_code,
        external_id=product.external_id,
        document_type=document.document_type,
        content_hash=document.content_hash,
        previous_content_hash=previous_hash,
        document_changed=changed,
        chunk_count=len(chunks),
        chunk_sections=[c.section for c in chunks],
        persisted=persisted,
        document_inserted=document_inserted,
        chunk_sync=chunk_sync,
    )


def run_indexing(
    reader: ProductReader,
    *,
    product_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    repository: IndexingRepository | None = None,
) -> list[IndexingOutcome]:
    if not dry_run and repository is None:
        raise ValueError("repository is required when dry_run=False")

    ids = product_ids if product_ids is not None else reader.all_product_ids()
    if limit is not None:
        ids = ids[:limit]

    return [
        index_product(reader, product_id, dry_run=dry_run, repository=repository)
        for product_id in ids
    ]
