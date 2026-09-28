"""Persistence tests for indexing/repository.py against a real disposable
PostgreSQL+pgvector container (tests/run_db_tests.sh), applying the actual
Phase 1 migrations -- not a mock."""

from indexing.builder import build_document
from indexing.chunker import build_chunks
from indexing.repository import IndexingRepository, ProductReader

from .db_fixtures import requires_db, db_conn  # noqa: F401


def _insert_product(conn, product) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO products (
                source, external_id, model_code, name, brand, category, product_url,
                year, series, screen_size_inches, resolution, panel_technology, refresh_rate_hz,
                price, sale_price, currency, is_available, description, specs_text
            ) VALUES (
                %(source)s, %(external_id)s, %(model_code)s, %(name)s, %(brand)s, %(category)s,
                %(product_url)s, %(year)s, %(series)s, %(screen_size_inches)s, %(resolution)s,
                %(panel_technology)s, %(refresh_rate_hz)s, %(price)s, %(sale_price)s, %(currency)s,
                %(is_available)s, %(description)s, ''
            ) RETURNING id;
            """,
            {
                "source": product.source,
                "external_id": product.external_id,
                "model_code": product.model_code,
                "name": product.name,
                "brand": product.brand,
                "category": product.category,
                "product_url": product.product_url,
                "year": product.year,
                "series": product.series,
                "screen_size_inches": product.screen_size_inches,
                "resolution": product.resolution,
                "panel_technology": product.panel_technology,
                "refresh_rate_hz": product.refresh_rate_hz,
                "price": product.price,
                "sale_price": product.sale_price,
                "currency": product.currency,
                "is_available": product.is_available,
                "description": product.description,
            },
        )
        product_id = cur.fetchone()[0]
    conn.commit()
    return product_id


def _insert_specs(conn, product_id: int, specs) -> None:
    with conn.cursor() as cur:
        for spec in specs:
            cur.execute(
                """
                INSERT INTO product_specs (product_id, spec_group, spec_name, spec_key, spec_value, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (product_id, spec.spec_group, spec.spec_name, spec.spec_key, spec.spec_value, spec.sort_order),
            )
    conn.commit()


@requires_db
def test_insert_then_update_document_and_chunks(db_conn, oled_product_and_specs):
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)
    product = reader.get_product(product_id)
    read_specs = reader.get_specs(product_id)

    document = build_document(product, read_specs)
    chunks = build_chunks(product, read_specs)

    result1 = repo.save_document_with_chunks(product_id, document, chunks)
    assert result1.inserted is True
    assert result1.chunk_count == len(chunks)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM chunks WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == len(chunks)
        cur.execute("SELECT count(*) FROM chunks WHERE product_id = %s AND embedding IS NOT NULL", (product_id,))
        assert cur.fetchone()[0] == 0  # embedding must stay NULL

    # Re-run (update path) -- must not duplicate.
    result2 = repo.save_document_with_chunks(product_id, document, chunks)
    assert result2.inserted is False
    assert result2.document_id == result1.document_id

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM chunks WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == len(chunks)


@requires_db
def test_chunks_product_id_synced_by_trigger(db_conn, oled_product_and_specs):
    """chunks.product_id is denormalized from documents.product_id by a
    Phase 1 trigger (db/migrations/004_rag.sql) -- confirm it actually
    fires rather than assuming."""
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)
    product = reader.get_product(product_id)
    read_specs = reader.get_specs(product_id)
    document = build_document(product, read_specs)
    chunks = build_chunks(product, read_specs)
    repo.save_document_with_chunks(product_id, document, chunks)

    with db_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT product_id FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE product_id = %s)", (product_id,))
        rows = cur.fetchall()
    assert rows == [(product_id,)]


@requires_db
def test_cascade_delete_product_removes_document_and_chunks(db_conn, oled_product_and_specs):
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)
    product = reader.get_product(product_id)
    read_specs = reader.get_specs(product_id)
    document = build_document(product, read_specs)
    chunks = build_chunks(product, read_specs)
    repo.save_document_with_chunks(product_id, document, chunks)

    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM chunks WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == 0


@requires_db
def test_product_isolation_two_products_separate_documents(db_conn, oled_product_and_specs, neo_qled_product_and_specs):
    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)

    ids = []
    for product_dataclass, specs in (oled_product_and_specs, neo_qled_product_and_specs):
        pid = _insert_product(db_conn, product_dataclass)
        _insert_specs(db_conn, pid, specs)
        product = reader.get_product(pid)
        read_specs = reader.get_specs(pid)
        document = build_document(product, read_specs)
        chunks = build_chunks(product, read_specs)
        repo.save_document_with_chunks(pid, document, chunks)
        ids.append(pid)

    with db_conn.cursor() as cur:
        cur.execute("SELECT product_id, count(*) FROM chunks GROUP BY product_id ORDER BY product_id")
        rows = cur.fetchall()
    assert {r[0] for r in rows} == set(ids)
    assert len(rows) == 2
