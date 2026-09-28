"""Running the same catalog fixture twice through the full service pipeline
must not duplicate products or product_specs -- the acceptance criterion
from the task brief, exercised end-to-end rather than at the repository
layer alone."""

from ingestion.http import HttpResponse
from ingestion.repository import IngestionRunRepository, ProductRepository
from ingestion.service import run_ingestion

from .db_fixtures import requires_db, db_conn  # noqa: F401

CATALOG_URL = "https://galaxystore.ru/catalog/televizory/year=2026/"
PRODUCT_URL = "https://galaxystore.ru/product/UE50M70HAUXPY/"


class _FakeHttpClient:
    def __init__(self, responses: dict[str, str]):
        self._responses = responses

    def get(self, url: str) -> HttpResponse:
        return HttpResponse(url=url, status_code=200, text=self._responses[url])


@requires_db
def test_running_same_catalog_fixture_twice_is_idempotent(
    db_conn, catalog_page1_html, product_miniled_html
):
    # Single-product catalog page so the run is small and deterministic:
    # only serve the one product URL the fake catalog page points at.
    responses = {CATALOG_URL: catalog_page1_html, PRODUCT_URL: product_miniled_html}
    client = _FakeHttpClient(responses)
    product_repo = ProductRepository(db_conn)
    run_repo = IngestionRunRepository(db_conn)

    def run_once():
        return run_ingestion(
            client,
            source="galaxystore",
            source_url=CATALOG_URL,
            max_pages=1,
            limit_products=1,
            dry_run=False,
            product_repo=product_repo,
            run_repo=run_repo,
        )

    summary1 = run_once()
    assert summary1.products_inserted == 1
    assert summary1.status == "success"

    summary2 = run_once()
    assert summary2.products_updated == 1
    assert summary2.products_inserted == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM products WHERE source = 'galaxystore'")
        assert cur.fetchone()[0] == 1

        cur.execute(
            "SELECT count(*) FROM product_specs ps JOIN products p ON p.id = ps.product_id "
            "WHERE p.source = 'galaxystore'"
        )
        specs_count = cur.fetchone()[0]

        cur.execute(
            "SELECT count(DISTINCT spec_key) FROM product_specs ps JOIN products p ON p.id = ps.product_id "
            "WHERE p.source = 'galaxystore'"
        )
        distinct_keys = cur.fetchone()[0]
        assert specs_count == distinct_keys  # no duplicate spec rows

        cur.execute("SELECT count(*) FROM ingestion_runs")
        assert cur.fetchone()[0] == 2  # two runs, one per run_once() call
