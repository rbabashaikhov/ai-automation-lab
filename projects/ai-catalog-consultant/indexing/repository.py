"""PostgreSQL persistence against the existing Phase 1 `documents`/`chunks`
schema -- no schema changes.

## Upsert strategy: documents

`INSERT ... ON CONFLICT (product_id, document_type) DO UPDATE` (the
schema's own enforced identity: at most one *current* row per product per
type -- see db/migrations/004_rag.sql and db/README.md "Re-indexing",
read directly rather than assumed) always preserves the document's `id`.
The `DO UPDATE ... WHERE` clause additionally skips the write entirely
when neither `content_hash` nor `metadata` actually changed, so rebuilding
an unchanged product doesn't even bump `updated_at` (`set_updated_at()`'s
trigger only fires on rows an `UPDATE` statement actually touches).

## Upsert strategy: chunks (Phase 3B.1 -- embedding-aware sync)

Phase 3A originally deleted and re-inserted every one of a document's
chunks on every rebuild, unconditionally discarding any embedding a
chunk already had -- safe for "no embeddings exist yet" but wrong the
moment embeddings do exist (see `docs/adr/003-embedding-aware-chunk-sync.md`
for the full root-cause writeup and the alternatives considered).
`sync_chunks` replaces that: it matches each newly-built chunk against
the document's *existing* chunks by **logical section**
(`metadata->>'section'`, e.g. `"gaming"` -- already a stable, deterministic
identity per chunk per document by construction, see `chunker.py`
`SECTION_ORDER`; never array position), then per matched pair:

- **same `content_hash`** -> the existing row is updated in place
  (`chunk_index`, `metadata` only) and its `embedding`/`embedding_model`
  are **never referenced in the SQL at all** -- there is no way for this
  code to clear a value it never touches, which is the actual guarantee,
  not just an intention.
- **different `content_hash`** -> the existing row is updated
  (`content`, `content_hash`, `metadata`, `chunk_index`) *and*
  `embedding` is explicitly reset to `NULL` (`embedding_model` reset to
  its schema default -- see "`embedding_model` caveat" below), so the
  n8n indexing workflow picks it back up on its next run.
- **no existing row for that section** -> a fresh row is inserted,
  `embedding` absent (`NULL` by omission).
- **an existing row's section no longer appears in the new build** -> that
  row is deleted (a stale logical chunk).

`content_hash` is the sole authority for "does the existing embedding
still represent this content" -- never `embedding IS NOT NULL` (an
already-cleared or never-embedded chunk must still go through the exact
same content-hash comparison, not be special-cased).

### Avoiding `UNIQUE (document_id, chunk_index)` collisions mid-sync

If the *set* of sections present changes (a section is added or removed
somewhere before the end of `SECTION_ORDER`), later sections' target
`chunk_index` shifts. Applying those shifts as naive per-row `UPDATE`s in
arbitrary order can transiently collide with another still-live row's
current `chunk_index` and violate the unique constraint. `sync_chunks`
avoids this without touching the schema (no deferrable constraint) by
first moving every surviving row whose index will change to a guaranteed-
unique negative placeholder (`-id`; real `chunk_index` values are always
`>= 0`), then assigning final positions in a second pass -- so no two live
rows ever simultaneously hold the same `chunk_index`.

## `embedding_model` caveat (carried over from Phase 3A)

The schema declares `embedding_model` `NOT NULL DEFAULT
'text-embedding-3-small'` (db/migrations/004_rag.sql) -- there is no
legal way to store NULL there. A newly-inserted or invalidated chunk gets
that default (via column omission on INSERT, `= DEFAULT` on UPDATE), which
is **not** a claim that an embedding of that model exists for it --
`embedding IS NULL` remains the only authoritative "no embedding yet"
signal, exactly as documented in Phase 3A; nothing about this phase's fix
changes that.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

import psycopg2

from .builder import DocumentDraft
from .chunker import ChunkDraft
from .models import Product, Spec

_PRODUCT_COLUMNS = (
    "id, source, external_id, model_code, name, brand, category, product_url, "
    "year, series, screen_size_inches, resolution, panel_technology, refresh_rate_hz, "
    "price, sale_price, currency, is_available, description"
)

_SELECT_ALL_PRODUCT_IDS_SQL = "SELECT id FROM products ORDER BY id;"
_SELECT_PRODUCT_BY_ID_SQL = f"SELECT {_PRODUCT_COLUMNS} FROM products WHERE id = %s;"
_SELECT_SPECS_SQL = (
    "SELECT spec_group, spec_name, spec_key, spec_value, sort_order "
    "FROM product_specs WHERE product_id = %s ORDER BY sort_order;"
)

_UPSERT_DOCUMENT_SQL = """
INSERT INTO documents (product_id, document_type, title, content, content_hash, metadata)
VALUES (%(product_id)s, %(document_type)s, %(title)s, %(content)s, %(content_hash)s, %(metadata)s)
ON CONFLICT (product_id, document_type) DO UPDATE SET
    title = EXCLUDED.title,
    content = EXCLUDED.content,
    content_hash = EXCLUDED.content_hash,
    metadata = EXCLUDED.metadata
WHERE documents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
   OR documents.metadata IS DISTINCT FROM EXCLUDED.metadata
RETURNING id, (xmax = 0) AS inserted;
"""

# Fallback used only when the WHERE-guarded upsert above finds nothing to
# insert or update (a true no-op rebuild) -- ON CONFLICT DO UPDATE ... WHERE
# returns no row at all when the WHERE condition is false, so the caller
# still needs the existing row's id.
_SELECT_DOCUMENT_ID_SQL = """
SELECT id FROM documents WHERE product_id = %(product_id)s AND document_type = %(document_type)s;
"""

_SELECT_EXISTING_CHUNKS_SQL = """
SELECT id, metadata->>'section' AS section, content_hash, chunk_index
FROM chunks WHERE document_id = %s;
"""

_DELETE_CHUNKS_BY_ID_SQL = "DELETE FROM chunks WHERE id = ANY(%s);"

_PARK_CHUNK_INDEX_SQL = "UPDATE chunks SET chunk_index = -id WHERE id = ANY(%s);"

_UPDATE_UNCHANGED_CHUNK_SQL = """
UPDATE chunks SET chunk_index = %(chunk_index)s, metadata = %(metadata)s
WHERE id = %(id)s;
"""

_UPDATE_CHANGED_CHUNK_SQL = """
UPDATE chunks SET
    chunk_index = %(chunk_index)s,
    content = %(content)s,
    content_hash = %(content_hash)s,
    metadata = %(metadata)s,
    embedding = NULL,
    embedding_model = DEFAULT
WHERE id = %(id)s;
"""

_INSERT_CHUNK_SQL = """
INSERT INTO chunks (document_id, chunk_index, content, content_hash, metadata)
VALUES (%(document_id)s, %(chunk_index)s, %(content)s, %(content_hash)s, %(metadata)s);
"""


def connect(database_url: str):
    return psycopg2.connect(database_url)


@contextmanager
def transaction(conn) -> Iterator[None]:
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@dataclass
class ChunkSyncResult:
    preserved: int = 0  # unchanged content_hash: row kept, embedding untouched
    invalidated: int = 0  # changed content_hash: row updated, embedding cleared
    inserted: int = 0  # new logical section: fresh row, embedding NULL
    deleted: int = 0  # stale logical section: row removed

    @property
    def total(self) -> int:
        return self.preserved + self.invalidated + self.inserted


@dataclass
class UpsertResult:
    document_id: int
    inserted: bool
    chunk_count: int
    chunk_sync: ChunkSyncResult = field(default_factory=ChunkSyncResult)


class IndexingRepository:
    def __init__(self, conn):
        self._conn = conn

    def upsert_document(self, product_id: int, draft: DocumentDraft) -> tuple[int, bool]:
        params = {
            "product_id": product_id,
            "document_type": draft.document_type,
            "title": draft.title,
            "content": draft.content,
            "content_hash": draft.content_hash,
            "metadata": json.dumps(draft.metadata, ensure_ascii=False),
        }
        with self._conn.cursor() as cur:
            cur.execute(_UPSERT_DOCUMENT_SQL, params)
            row = cur.fetchone()
            if row is not None:
                return row[0], row[1]
            # WHERE guard found nothing to change -- true no-op rebuild.
            # The row (and its id) already exists; just look it up.
            cur.execute(
                _SELECT_DOCUMENT_ID_SQL,
                {"product_id": product_id, "document_type": draft.document_type},
            )
            document_id = cur.fetchone()[0]
        return document_id, False

    def sync_chunks(self, document_id: int, chunks: list[ChunkDraft]) -> ChunkSyncResult:
        """Embedding-aware chunk sync -- see module docstring "Upsert
        strategy: chunks" for the full algorithm and why."""
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_EXISTING_CHUNKS_SQL, (document_id,))
            existing_by_section = {
                section: {"id": row_id, "content_hash": content_hash, "chunk_index": chunk_index}
                for row_id, section, content_hash, chunk_index in cur.fetchall()
            }

        new_sections = {chunk.section for chunk in chunks}
        stale_ids = [
            row["id"]
            for section, row in existing_by_section.items()
            if section not in new_sections
        ]

        result = ChunkSyncResult(deleted=len(stale_ids))

        with self._conn.cursor() as cur:
            if stale_ids:
                cur.execute(_DELETE_CHUNKS_BY_ID_SQL, (stale_ids,))

            # Park every surviving row whose target chunk_index differs from
            # its current one at a guaranteed-unique negative placeholder
            # first, so the second pass below can never collide with a
            # still-live row on UNIQUE (document_id, chunk_index).
            to_park = [
                existing["id"]
                for chunk in chunks
                if (existing := existing_by_section.get(chunk.section)) is not None
                and existing["chunk_index"] != chunk.chunk_index
            ]
            if to_park:
                cur.execute(_PARK_CHUNK_INDEX_SQL, (to_park,))

            for chunk in chunks:
                metadata_json = json.dumps(chunk.metadata, ensure_ascii=False)
                existing = existing_by_section.get(chunk.section)

                if existing is None:
                    cur.execute(
                        _INSERT_CHUNK_SQL,
                        {
                            "document_id": document_id,
                            "chunk_index": chunk.chunk_index,
                            "content": chunk.content,
                            "content_hash": chunk.content_hash,
                            "metadata": metadata_json,
                        },
                    )
                    result.inserted += 1
                elif existing["content_hash"] == chunk.content_hash:
                    cur.execute(
                        _UPDATE_UNCHANGED_CHUNK_SQL,
                        {"chunk_index": chunk.chunk_index, "metadata": metadata_json, "id": existing["id"]},
                    )
                    result.preserved += 1
                else:
                    cur.execute(
                        _UPDATE_CHANGED_CHUNK_SQL,
                        {
                            "chunk_index": chunk.chunk_index,
                            "content": chunk.content,
                            "content_hash": chunk.content_hash,
                            "metadata": metadata_json,
                            "id": existing["id"],
                        },
                    )
                    result.invalidated += 1

        return result

    def save_document_with_chunks(
        self, product_id: int, document: DocumentDraft, chunks: list[ChunkDraft]
    ) -> UpsertResult:
        with transaction(self._conn):
            document_id, inserted = self.upsert_document(product_id, document)
            chunk_sync = self.sync_chunks(document_id, chunks)
        return UpsertResult(
            document_id=document_id, inserted=inserted, chunk_count=chunk_sync.total, chunk_sync=chunk_sync
        )


class ProductReader:
    """Read-only access to `products` / `product_specs` -- the indexing
    role has no INSERT/UPDATE/DELETE grant on either (see
    projects/ai-catalog-consultant/README.md "Database access"), so this
    class only ever issues SELECTs."""

    def __init__(self, conn):
        self._conn = conn

    def all_product_ids(self) -> list[int]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_ALL_PRODUCT_IDS_SQL)
            return [row[0] for row in cur.fetchall()]

    def get_product(self, product_id: int) -> Product | None:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_PRODUCT_BY_ID_SQL, (product_id,))
            row = cur.fetchone()
        if row is None:
            return None
        (
            id_, source, external_id, model_code, name, brand, category, product_url,
            year, series, screen_size_inches, resolution, panel_technology, refresh_rate_hz,
            price, sale_price, currency, is_available, description,
        ) = row
        return Product(
            id=id_,
            source=source,
            external_id=external_id,
            model_code=model_code,
            name=name,
            brand=brand,
            category=category,
            product_url=product_url,
            year=year,
            series=series,
            screen_size_inches=float(screen_size_inches) if screen_size_inches is not None else None,
            resolution=resolution,
            panel_technology=panel_technology,
            refresh_rate_hz=refresh_rate_hz,
            price=float(price) if price is not None else None,
            sale_price=float(sale_price) if sale_price is not None else None,
            currency=currency,
            is_available=is_available,
            description=description,
        )

    def get_existing_document_hash(self, product_id: int, document_type: str) -> str | None:
        """Current `content_hash` for this product's document, if one exists
        yet -- used only for change-detection reporting (see service.py),
        including in --dry-run, since it's a plain SELECT."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT content_hash FROM documents WHERE product_id = %s AND document_type = %s",
                (product_id, document_type),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def get_specs(self, product_id: int) -> list[Spec]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_SPECS_SQL, (product_id,))
            rows = cur.fetchall()
        return [
            Spec(spec_group=g, spec_name=n, spec_key=k, spec_value=v, sort_order=so)
            for g, n, k, v, so in rows
        ]
