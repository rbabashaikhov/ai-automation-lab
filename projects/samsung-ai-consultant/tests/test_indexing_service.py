"""Service-level change-detection + idempotency tests against a real
disposable PostgreSQL+pgvector container."""

from indexing.repository import IndexingRepository, ProductReader
from indexing.service import index_product, run_indexing

from .db_fixtures import requires_db, db_conn  # noqa: F401
from .test_indexing_repository import _insert_product, _insert_specs


@requires_db
def test_first_index_reports_no_previous_hash_and_changed(db_conn, oled_product_and_specs):
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)

    outcome = index_product(reader, product_id, dry_run=False, repository=repo)
    assert outcome.previous_content_hash is None
    assert outcome.document_changed is True
    assert outcome.document_inserted is True


@requires_db
def test_second_index_same_data_reports_unchanged_and_is_idempotent(db_conn, oled_product_and_specs):
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)

    outcome1 = index_product(reader, product_id, dry_run=False, repository=repo)
    outcome2 = index_product(reader, product_id, dry_run=False, repository=repo)

    assert outcome2.previous_content_hash == outcome1.content_hash
    assert outcome2.document_changed is False
    assert outcome2.document_inserted is False
    assert outcome2.content_hash == outcome1.content_hash

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM chunks WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == outcome1.chunk_count


@requires_db
def test_dry_run_reports_change_detection_without_writing(db_conn, oled_product_and_specs):
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)

    outcome = index_product(reader, product_id, dry_run=True, repository=None)
    assert outcome.persisted is False
    assert outcome.document_inserted is None

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == 0  # dry-run never writes
        cur.execute("SELECT count(*) FROM chunks WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == 0


@requires_db
def test_spec_change_is_detected_on_next_index(db_conn, oled_product_and_specs):
    product_dataclass, specs = oled_product_and_specs
    product_id = _insert_product(db_conn, product_dataclass)
    _insert_specs(db_conn, product_id, specs)

    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)
    outcome1 = index_product(reader, product_id, dry_run=False, repository=repo)

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE product_specs SET spec_value = 'Нет' "
            "WHERE product_id = %s AND spec_group = 'Игровой режим' AND spec_name = 'Игровой режим'",
            (product_id,),
        )
    db_conn.commit()

    outcome2 = index_product(reader, product_id, dry_run=False, repository=repo)
    assert outcome2.document_changed is True
    assert outcome2.content_hash != outcome1.content_hash
    assert outcome2.document_inserted is False  # still an update, not a new row


@requires_db
def test_run_indexing_respects_limit_and_is_idempotent_across_products(
    db_conn, oled_product_and_specs, neo_qled_product_and_specs, unusual_display_product_and_specs
):
    reader = ProductReader(db_conn)
    repo = IndexingRepository(db_conn)

    for product_dataclass, specs in (
        oled_product_and_specs, neo_qled_product_and_specs, unusual_display_product_and_specs
    ):
        pid = _insert_product(db_conn, product_dataclass)
        _insert_specs(db_conn, pid, specs)

    outcomes1 = run_indexing(reader, dry_run=False, repository=repo)
    assert len(outcomes1) == 3
    assert all(o.document_inserted for o in outcomes1)

    outcomes2 = run_indexing(reader, dry_run=False, repository=repo)
    assert all(o.document_inserted is False for o in outcomes2)
    assert all(o.document_changed is False for o in outcomes2)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents")
        assert cur.fetchone()[0] == 3
