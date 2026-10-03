"""Regression tests for Phase 3B.1's embedding-aware chunk sync
(indexing/repository.py `sync_chunks`), against a real disposable
PostgreSQL+pgvector container. These prove the exact defect Phase 3A had:
rebuilding a product used to delete-and-reinsert every chunk unconditionally,
discarding any embedding it already had.

Embeddings here are synthetic, deterministic, locally-generated vectors
(never a real OpenAI call) -- these tests only need to prove that a stored
vector is or isn't preserved byte-for-byte across a rebuild, not that it's
semantically meaningful. Never printed in full; compared via a SHA-256
fingerprint of Postgres's own text representation of the vector.
"""

from __future__ import annotations

import dataclasses
import hashlib

from indexing.builder import build_document
from indexing.chunker import build_chunks
from indexing.repository import IndexingRepository, ProductReader

from .db_fixtures import requires_db, db_conn  # noqa: F401
from .test_indexing_repository import _insert_product, _insert_specs


def _fake_vector(seed: int) -> list[float]:
    """A deterministic, distinguishable-per-seed 1536-dim vector -- not a
    real embedding, just something reproducible to compare before/after."""
    return [round(((seed * 37 + i * 7) % 997) / 997.0, 6) for i in range(1536)]


def _set_fake_embedding(conn, chunk_id: int, seed: int, model: str = "text-embedding-3-small") -> None:
    literal = "[" + ",".join(str(v) for v in _fake_vector(seed)) + "]"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE chunks SET embedding = %s::vector, embedding_model = %s WHERE id = %s",
            (literal, model, chunk_id),
        )
    conn.commit()


def _embedding_fingerprint(conn, chunk_id: int) -> str | None:
    """SHA-256 of Postgres's own text rendering of the stored vector --
    never returns or logs the vector itself."""
    with conn.cursor() as cur:
        cur.execute("SELECT embedding::text FROM chunks WHERE id = %s", (chunk_id,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return hashlib.sha256(row[0].encode("utf-8")).hexdigest()


def _chunks_by_section(conn, document_id: int) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, metadata->>'section', content_hash, chunk_index, "
            "embedding IS NOT NULL, embedding_model "
            "FROM chunks WHERE document_id = %s",
            (document_id,),
        )
        rows = cur.fetchall()
    return {
        section: {
            "id": row_id,
            "content_hash": content_hash,
            "chunk_index": chunk_index,
            "has_embedding": has_embedding,
            "embedding_model": embedding_model,
        }
        for row_id, section, content_hash, chunk_index, has_embedding, embedding_model in rows
    }


def _build_and_save(repo, reader, product_id, specs_override=None):
    product = reader.get_product(product_id)
    specs = specs_override if specs_override is not None else reader.get_specs(product_id)
    document = build_document(product, specs)
    chunks = build_chunks(product, specs)
    result = repo.save_document_with_chunks(product_id, document, chunks)
    return result, document, chunks


@requires_db
def test_unchanged_rebuild_preserves_document_and_chunk_identity_and_embeddings(
    db_conn, oled_product_and_specs
):
    """Covers regression items 1-4, 12: unchanged document/chunk identity
    preserved, exact embedding + embedding_model preserved, no duplicates
    across repeated rebuilds."""
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)

    result1, _, chunks1 = _build_and_save(repo, reader, product_id)
    document_id = result1.document_id

    with db_conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM documents WHERE id = %s", (document_id,))
        updated_at_1 = cur.fetchone()[0]

    before = _chunks_by_section(db_conn, document_id)
    for i, (section, row) in enumerate(before.items()):
        _set_fake_embedding(db_conn, row["id"], seed=i + 1)
    fingerprints_before = {
        section: _embedding_fingerprint(db_conn, row["id"]) for section, row in before.items()
    }

    # Rebuild twice more with byte-identical content -- must be a total no-op
    # for every chunk and for the document row itself.
    for _ in range(2):
        result, _, chunks_n = _build_and_save(repo, reader, product_id)
        assert result.document_id == document_id
        assert result.inserted is False
        assert result.chunk_sync.preserved == len(chunks_n)
        assert result.chunk_sync.invalidated == 0
        assert result.chunk_sync.inserted == 0
        assert result.chunk_sync.deleted == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM documents WHERE id = %s", (document_id,))
        updated_at_2 = cur.fetchone()[0]
    assert updated_at_2 == updated_at_1, "no-op rebuild must not touch the document row at all"

    after = _chunks_by_section(db_conn, document_id)
    assert set(after) == set(before)
    for section, row in before.items():
        assert after[section]["id"] == row["id"], f"{section}: chunk row identity not preserved"
        assert after[section]["has_embedding"] is True
        assert after[section]["embedding_model"] == "text-embedding-3-small"
        assert _embedding_fingerprint(db_conn, after[section]["id"]) == fingerprints_before[section], (
            f"{section}: embedding vector changed on a content-unchanged rebuild"
        )

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (document_id,))
        assert cur.fetchone()[0] == len(chunks1)
        cur.execute("SELECT count(*) FROM documents WHERE id = %s", (document_id,))
        assert cur.fetchone()[0] == 1


@requires_db
def test_changed_chunk_invalidates_only_that_chunk(db_conn, oled_product_and_specs):
    """Covers regression items 5-9: changed chunk keeps its row identity
    where practical, but content/content_hash update and embedding/
    embedding_model are cleared; unrelated chunks keep their embeddings
    exactly. Never touches the real 75 production products/specs -- this
    mutates a row in the disposable test container only."""
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)

    result1, _, _ = _build_and_save(repo, reader, product_id)
    document_id = result1.document_id

    before = _chunks_by_section(db_conn, document_id)
    assert "gaming" in before, "fixture must have a gaming section to mutate"
    for i, (section, row) in enumerate(before.items()):
        _set_fake_embedding(db_conn, row["id"], seed=i + 1)
    fingerprints_before = {
        section: _embedding_fingerprint(db_conn, row["id"]) for section, row in before.items()
    }

    # Mutate one gaming-section spec value in the disposable DB directly
    # (source-of-truth for the next rebuild's read), never the real
    # samsung_rag production product_specs.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE product_specs SET spec_value = 'Нет' "
            "WHERE product_id = %s AND spec_group = 'Игровой режим' AND spec_name = 'Игровой режим'",
            (product_id,),
        )
    db_conn.commit()

    result2, _, chunks2 = _build_and_save(repo, reader, product_id)
    assert result2.document_id == document_id
    assert result2.chunk_sync.invalidated == 1
    assert result2.chunk_sync.preserved == len(chunks2) - 1
    assert result2.chunk_sync.inserted == 0
    assert result2.chunk_sync.deleted == 0

    after = _chunks_by_section(db_conn, document_id)

    # The changed chunk: same row id (identity kept where practical),
    # different content_hash, embedding cleared.
    assert after["gaming"]["id"] == before["gaming"]["id"]
    assert after["gaming"]["content_hash"] != before["gaming"]["content_hash"]
    assert after["gaming"]["has_embedding"] is False
    assert after["gaming"]["embedding_model"] == "text-embedding-3-small"  # schema default, not a claim of an embedding

    # Every other chunk: untouched, exact same embedding.
    for section in before:
        if section == "gaming":
            continue
        assert after[section]["id"] == before[section]["id"]
        assert after[section]["content_hash"] == before[section]["content_hash"]
        assert after[section]["has_embedding"] is True
        assert _embedding_fingerprint(db_conn, after[section]["id"]) == fingerprints_before[section]


@requires_db
def test_new_logical_chunk_gets_null_embedding_and_reindexes_safely(db_conn, oled_product_and_specs):
    """Covers regression item 10, plus exercises the chunk_index
    reassignment path (park-then-reassign) when a previously-absent
    section is reintroduced, shifting every later section's index."""
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)

    specs_without_gaming = [s for s in specs if s.spec_group != "Игровой режим"]
    _insert_specs(db_conn, product_id, specs_without_gaming)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)

    result1, _, chunks1 = _build_and_save(repo, reader, product_id, specs_override=specs_without_gaming)
    document_id = result1.document_id
    assert "gaming" not in {c.section for c in chunks1}

    before = _chunks_by_section(db_conn, document_id)
    for i, (section, row) in enumerate(before.items()):
        _set_fake_embedding(db_conn, row["id"], seed=i + 1)
    fingerprints_before = {
        section: _embedding_fingerprint(db_conn, row["id"]) for section, row in before.items()
    }

    # Re-insert the gaming specs into the disposable DB and rebuild from
    # the full spec set -- "gaming" is now a brand-new logical chunk.
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM product_specs WHERE product_id = %s", (product_id,))
    db_conn.commit()
    _insert_specs(db_conn, product_id, specs)

    result2, _, chunks2 = _build_and_save(repo, reader, product_id)
    assert result2.chunk_sync.inserted == 1
    assert result2.chunk_sync.preserved == len(before)
    assert result2.chunk_sync.deleted == 0

    after = _chunks_by_section(db_conn, document_id)
    assert "gaming" in after
    assert after["gaming"]["has_embedding"] is False

    # No UNIQUE (document_id, chunk_index) violation, and every previously
    # existing (now-shifted) chunk kept its row id and embedding.
    indexes = [row["chunk_index"] for row in after.values()]
    assert len(indexes) == len(set(indexes)), "chunk_index collision after reindex"
    for section, row in before.items():
        assert after[section]["id"] == row["id"]
        assert after[section]["has_embedding"] is True
        assert _embedding_fingerprint(db_conn, after[section]["id"]) == fingerprints_before[section]

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (document_id,))
        assert cur.fetchone()[0] == len(chunks2)


@requires_db
def test_removed_logical_chunk_is_deleted_and_others_reindex_safely(db_conn, oled_product_and_specs):
    """Covers regression item 11 (stale chunk deletion) and the reverse
    reindex direction from the insertion test above."""
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)

    result1, _, chunks1 = _build_and_save(repo, reader, product_id)
    document_id = result1.document_id
    assert "gaming" in {c.section for c in chunks1}

    before = _chunks_by_section(db_conn, document_id)
    for i, (section, row) in enumerate(before.items()):
        _set_fake_embedding(db_conn, row["id"], seed=i + 1)
    fingerprints_before = {
        section: _embedding_fingerprint(db_conn, row["id"]) for section, row in before.items()
    }
    gaming_chunk_id = before["gaming"]["id"]

    specs_without_gaming = [s for s in specs if s.spec_group != "Игровой режим"]
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM product_specs WHERE product_id = %s", (product_id,))
    db_conn.commit()
    _insert_specs(db_conn, product_id, specs_without_gaming)

    result2, _, chunks2 = _build_and_save(repo, reader, product_id, specs_override=specs_without_gaming)
    assert result2.chunk_sync.deleted == 1
    assert result2.chunk_sync.inserted == 0
    assert result2.chunk_sync.preserved == len(before) - 1

    after = _chunks_by_section(db_conn, document_id)
    assert "gaming" not in after

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE id = %s", (gaming_chunk_id,))
        assert cur.fetchone()[0] == 0  # actually deleted, not just orphaned

    indexes = [row["chunk_index"] for row in after.values()]
    assert len(indexes) == len(set(indexes)), "chunk_index collision after reindex"
    for section, row in before.items():
        if section == "gaming":
            continue
        assert after[section]["id"] == row["id"]
        assert after[section]["has_embedding"] is True
        assert _embedding_fingerprint(db_conn, after[section]["id"]) == fingerprints_before[section]

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (document_id,))
        assert cur.fetchone()[0] == len(chunks2)


@requires_db
def test_unrelated_product_untouched_by_sync(db_conn, oled_product_and_specs, neo_qled_product_and_specs):
    """Covers regression item 13: syncing one product's chunks must not
    touch another product's document/chunks/embeddings at all."""
    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)

    product_a, specs_a = oled_product_and_specs
    product_a_id = _insert_product(db_conn, product_a)
    _insert_specs(db_conn, product_a_id, specs_a)
    result_a, _, _ = _build_and_save(repo, reader, product_a_id)

    product_b, specs_b = neo_qled_product_and_specs
    product_b_id = _insert_product(db_conn, product_b)
    _insert_specs(db_conn, product_b_id, specs_b)
    result_b, _, _ = _build_and_save(repo, reader, product_b_id)

    before_a = _chunks_by_section(db_conn, result_a.document_id)
    for i, (section, row) in enumerate(before_a.items()):
        _set_fake_embedding(db_conn, row["id"], seed=i + 1)
    fingerprints_a_before = {
        section: _embedding_fingerprint(db_conn, row["id"]) for section, row in before_a.items()
    }
    with db_conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM documents WHERE id = %s", (result_a.document_id,))
        doc_a_updated_at_before = cur.fetchone()[0]

    # Rebuild product B only -- twice, once identical and once with a real change.
    _build_and_save(repo, reader, product_b_id)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE product_specs SET spec_value = 'Нет' "
            "WHERE product_id = %s AND spec_group = 'Игровой режим' AND spec_name = 'Игровой режим'",
            (product_b_id,),
        )
    db_conn.commit()
    _build_and_save(repo, reader, product_b_id)

    after_a = _chunks_by_section(db_conn, result_a.document_id)
    assert set(after_a) == set(before_a)
    for section, row in before_a.items():
        assert after_a[section]["id"] == row["id"]
        assert after_a[section]["has_embedding"] is True
        assert _embedding_fingerprint(db_conn, after_a[section]["id"]) == fingerprints_a_before[section]

    with db_conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM documents WHERE id = %s", (result_a.document_id,))
        assert cur.fetchone()[0] == doc_a_updated_at_before
