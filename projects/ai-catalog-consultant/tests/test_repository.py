import time

from ingestion.catalog import CatalogEntry
from ingestion.product import extract_product
from ingestion.repository import IngestionRunRepository, ProductRepository

from .db_fixtures import requires_db, db_conn  # noqa: F401


def _product(product_html: str, url: str, catalog_entry: CatalogEntry | None = None):
    return extract_product(
        source="galaxystore", product_url=url, page_html=product_html, catalog_entry=catalog_entry
    )


@requires_db
def test_insert_then_update_preserves_first_seen_bumps_last_seen(db_conn, product_miniled_html):
    repo = ProductRepository(db_conn)
    product = _product(product_miniled_html, "https://galaxystore.ru/product/UE50M70HAUXPY/")

    result1 = repo.save_product(product)
    assert result1.inserted is True

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT first_seen_at, last_seen_at FROM products WHERE id = %s", (result1.product_id,)
        )
        first_seen_1, last_seen_1 = cur.fetchone()

    time.sleep(1.1)  # ensure now() advances at second precision

    result2 = repo.save_product(product)
    assert result2.inserted is False
    assert result2.product_id == result1.product_id

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT first_seen_at, last_seen_at FROM products WHERE id = %s", (result2.product_id,)
        )
        first_seen_2, last_seen_2 = cur.fetchone()

    assert first_seen_2 == first_seen_1
    assert last_seen_2 > last_seen_1


@requires_db
def test_upsert_does_not_duplicate_product_rows(db_conn, product_miniled_html):
    repo = ProductRepository(db_conn)
    product = _product(product_miniled_html, "https://galaxystore.ru/product/UE50M70HAUXPY/")

    repo.save_product(product)
    repo.save_product(product)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM products WHERE source = %s AND external_id = %s",
            (product.source, product.external_id),
        )
        assert cur.fetchone()[0] == 1


@requires_db
def test_product_specs_replaced_without_duplicates(db_conn, product_miniled_html):
    repo = ProductRepository(db_conn)
    product = _product(product_miniled_html, "https://galaxystore.ru/product/UE50M70HAUXPY/")

    result = repo.save_product(product)
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM product_specs WHERE product_id = %s", (result.product_id,))
        count_after_first = cur.fetchone()[0]
    assert count_after_first == len(product.spec_rows)

    repo.save_product(product)  # re-ingest the exact same product
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM product_specs WHERE product_id = %s", (result.product_id,))
        count_after_second = cur.fetchone()[0]
    assert count_after_second == count_after_first


@requires_db
def test_ingestion_run_lifecycle_and_error_recording(db_conn):
    run_repo = IngestionRunRepository(db_conn)
    run_id = run_repo.create_run(
        source="galaxystore", source_url="https://example.test/catalog/", metadata={"note": "test"}
    )
    assert run_id > 0

    run_repo.record_error(
        run_id,
        product_external_id="999",
        product_url="https://example.test/product/broken/",
        stage="fetch",
        error_code="500",
        error_message="server error",
    )

    run_repo.finish_run(
        run_id,
        status="partial",
        products_discovered=2,
        products_inserted=1,
        products_updated=0,
        products_failed=1,
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, products_discovered, products_inserted, products_failed, finished_at "
            "FROM ingestion_runs WHERE id = %s",
            (run_id,),
        )
        status, discovered, inserted, failed, finished_at = cur.fetchone()
        assert status == "partial"
        assert discovered == 2
        assert inserted == 1
        assert failed == 1
        assert finished_at is not None

        cur.execute("SELECT count(*) FROM ingestion_errors WHERE run_id = %s", (run_id,))
        assert cur.fetchone()[0] == 1
