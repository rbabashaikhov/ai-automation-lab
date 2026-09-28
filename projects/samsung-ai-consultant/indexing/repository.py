"""PostgreSQL persistence against the existing Phase 1 `documents`/`chunks`
schema -- no schema changes.

## Upsert / re-indexing strategy

`INSERT ... ON CONFLICT (product_id, document_type) DO UPDATE` for
`documents` (the schema's own enforced identity: at most one *current*
row per product per type -- see db/migrations/004_rag.sql and
db/README.md "Re-indexing", read directly rather than assumed). Chunks
are replaced wholesale per document per rebuild (delete-then-bulk-insert
in one transaction) -- see `chunker.py` module docstring "Stable chunk
identity" for why this is the safest strategy the current schema
supports.

## `embedding` / `embedding_model` for Phase 3A rows

`embedding` is left `NULL` by omitting it from the INSERT column list --
the column has no default and allows NULL, so this is unambiguous: every
chunk this module writes has `embedding IS NULL` until Phase 3B.

`embedding_model` is a different story: the schema declares it
`NOT NULL DEFAULT 'text-embedding-3-small'` (see
db/migrations/004_rag.sql) -- there is no legal way to store NULL there.
This module does not fight that constraint by inserting a placeholder
value of its own; it simply omits the column too, so the schema's own
default applies. That default string is **not** a claim that an
embedding exists for that model -- `embedding IS NULL` is the only
authoritative "no embedding yet" signal, and every query/report in this
phase checks `embedding`, never `embedding_model`, to determine that.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg2
import psycopg2.extras

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
RETURNING id, (xmax = 0) AS inserted;
"""

_DELETE_CHUNKS_SQL = "DELETE FROM chunks WHERE document_id = %s;"

_INSERT_CHUNKS_SQL = """
INSERT INTO chunks (document_id, chunk_index, content, content_hash, metadata)
VALUES %s
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
class UpsertResult:
    document_id: int
    inserted: bool
    chunk_count: int


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
            document_id, inserted = cur.fetchone()
        return document_id, inserted

    def replace_chunks(self, document_id: int, chunks: list[ChunkDraft]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(_DELETE_CHUNKS_SQL, (document_id,))
            if not chunks:
                return
            values = [
                (
                    document_id,
                    chunk.chunk_index,
                    chunk.content,
                    chunk.content_hash,
                    json.dumps(chunk.metadata, ensure_ascii=False),
                )
                for chunk in chunks
            ]
            psycopg2.extras.execute_values(cur, _INSERT_CHUNKS_SQL, values)

    def save_document_with_chunks(
        self, product_id: int, document: DocumentDraft, chunks: list[ChunkDraft]
    ) -> UpsertResult:
        with transaction(self._conn):
            document_id, inserted = self.upsert_document(product_id, document)
            self.replace_chunks(document_id, chunks)
        return UpsertResult(document_id=document_id, inserted=inserted, chunk_count=len(chunks))


class ProductReader:
    """Read-only access to `products` / `product_specs` -- the indexing
    role has no INSERT/UPDATE/DELETE grant on either (see
    projects/samsung-ai-consultant/README.md "Database access"), so this
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
