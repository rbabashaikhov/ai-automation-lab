"""PostgreSQL persistence against the Phase 1 schema.

Uses the existing `products` / `product_specs` / `ingestion_runs` /
`ingestion_errors` tables as-is (see
projects/ai-catalog-consultant/db/migrations/) -- no schema changes.

## Upsert / lifecycle

`upsert_product` is a single `INSERT ... ON CONFLICT (source, external_id)
DO UPDATE`, which is what makes `first_seen_at` (has a column default,
never referenced in the UPDATE SET list) stay fixed at its original value
across repeated ingestion, while `last_seen_at` is always bumped to `now()`
on every successful upsert -- both required by the task brief.

`product_specs` are replaced wholesale per product on every ingestion
(delete-then-bulk-insert in one transaction) rather than diffed row by
row. This is the simplest strategy that is provably idempotent and
duplicate-free -- a partial diff/merge would need its own identity and
staleness rules for zero benefit at this data volume (a few dozen spec
rows per product).

## Deactivation is deferred, by design

This module does **not** implement "mark products unseen in a complete
crawl as `is_available = false`". The task brief explicitly allows
deferring this as an unnecessary-risk lifecycle step in Phase 2, so no
deactivation function exists here -- see README "Known limitations" for
the reasoning. Every upsert still keeps each seen product's own
`is_available` current (from `normalize_availability`), so per-product
availability is always accurate for products actually re-scraped.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import psycopg2
import psycopg2.extras

from .normalize import SpecRow
from .product import ExtractedProduct

_UPSERT_PRODUCT_SQL = """
INSERT INTO products (
    source, external_id, sku, model_code, name, brand, product_url, category,
    year, series, screen_size_inches, resolution, panel_technology, refresh_rate_hz,
    price, sale_price, currency, stock_quantity, is_available,
    description, specs_text, source_hash, extra_attributes, raw_payload,
    last_seen_at
) VALUES (
    %(source)s, %(external_id)s, %(sku)s, %(model_code)s, %(name)s, %(brand)s,
    %(product_url)s, %(category)s, %(year)s, %(series)s, %(screen_size_inches)s,
    %(resolution)s, %(panel_technology)s, %(refresh_rate_hz)s, %(price)s,
    %(sale_price)s, %(currency)s, %(stock_quantity)s, %(is_available)s,
    %(description)s, %(specs_text)s, %(source_hash)s, %(extra_attributes)s,
    %(raw_payload)s, now()
)
ON CONFLICT (source, external_id) DO UPDATE SET
    sku = EXCLUDED.sku,
    model_code = EXCLUDED.model_code,
    name = EXCLUDED.name,
    brand = EXCLUDED.brand,
    product_url = EXCLUDED.product_url,
    category = EXCLUDED.category,
    year = EXCLUDED.year,
    series = EXCLUDED.series,
    screen_size_inches = EXCLUDED.screen_size_inches,
    resolution = EXCLUDED.resolution,
    panel_technology = EXCLUDED.panel_technology,
    refresh_rate_hz = EXCLUDED.refresh_rate_hz,
    price = EXCLUDED.price,
    sale_price = EXCLUDED.sale_price,
    currency = EXCLUDED.currency,
    stock_quantity = EXCLUDED.stock_quantity,
    is_available = EXCLUDED.is_available,
    description = EXCLUDED.description,
    specs_text = EXCLUDED.specs_text,
    source_hash = EXCLUDED.source_hash,
    extra_attributes = EXCLUDED.extra_attributes,
    raw_payload = EXCLUDED.raw_payload,
    last_seen_at = now()
RETURNING id, (xmax = 0) AS inserted;
"""

_DELETE_SPECS_SQL = "DELETE FROM product_specs WHERE product_id = %s;"

_INSERT_SPECS_SQL = """
INSERT INTO product_specs (
    product_id, spec_group, spec_name, spec_key, spec_value, normalized_value, unit, sort_order
) VALUES %s
"""

_CREATE_RUN_SQL = """
INSERT INTO ingestion_runs (source, source_url, status, metadata)
VALUES (%s, %s, 'running', %s)
RETURNING id;
"""

_FINISH_RUN_SQL = """
UPDATE ingestion_runs
SET finished_at = now(),
    status = %s,
    products_discovered = %s,
    products_inserted = %s,
    products_updated = %s,
    products_failed = %s,
    metadata = metadata || %s::jsonb
WHERE id = %s;
"""

_INSERT_ERROR_SQL = """
INSERT INTO ingestion_errors (
    run_id, product_external_id, product_url, stage, error_code, error_message, raw_payload
) VALUES (%s, %s, %s, %s, %s, %s, %s);
"""


@dataclass
class UpsertResult:
    product_id: int
    inserted: bool


def connect(database_url: str):
    """Open a psycopg2 connection. Caller is responsible for closing it."""
    return psycopg2.connect(database_url)


@contextmanager
def transaction(conn) -> Iterator[None]:
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class ProductRepository:
    def __init__(self, conn):
        self._conn = conn

    def upsert_product(self, product: ExtractedProduct) -> UpsertResult:
        params = {
            "source": product.source,
            "external_id": product.external_id,
            "sku": product.sku,
            "model_code": product.model_code,
            "name": product.name,
            "brand": product.brand,
            "product_url": product.product_url,
            "category": product.category,
            "year": product.year,
            "series": product.series,
            "screen_size_inches": product.screen_size_inches,
            "resolution": product.resolution,
            "panel_technology": product.panel_technology,
            "refresh_rate_hz": product.refresh_rate_hz,
            "price": product.price,
            "sale_price": product.sale_price,
            "currency": product.currency,
            "stock_quantity": product.stock_quantity,
            "is_available": product.is_available,
            "description": product.description,
            "specs_text": product.specs_text,
            "source_hash": product.source_hash,
            "extra_attributes": json.dumps(product.extra_attributes, ensure_ascii=False),
            "raw_payload": json.dumps(product.raw_payload, ensure_ascii=False, default=str),
        }
        with self._conn.cursor() as cur:
            cur.execute(_UPSERT_PRODUCT_SQL, params)
            row = cur.fetchone()
        return UpsertResult(product_id=row[0], inserted=row[1])

    def replace_product_specs(self, product_id: int, spec_rows: list[SpecRow]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(_DELETE_SPECS_SQL, (product_id,))
            if not spec_rows:
                return
            values = [
                (
                    product_id,
                    row.spec_group,
                    row.spec_name,
                    row.spec_key,
                    row.spec_value,
                    row.normalized_value,
                    row.unit,
                    row.sort_order,
                )
                for row in spec_rows
            ]
            psycopg2.extras.execute_values(cur, _INSERT_SPECS_SQL, values)

    def save_product(self, product: ExtractedProduct) -> UpsertResult:
        """Upsert a product and replace its specs in one transaction."""
        with transaction(self._conn):
            result = self.upsert_product(product)
            self.replace_product_specs(result.product_id, product.spec_rows)
        return result


class IngestionRunRepository:
    def __init__(self, conn):
        self._conn = conn

    def create_run(self, *, source: str, source_url: str | None, metadata: dict[str, Any]) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                _CREATE_RUN_SQL, (source, source_url, json.dumps(metadata, ensure_ascii=False))
            )
            run_id = cur.fetchone()[0]
        self._conn.commit()
        return run_id

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        products_discovered: int,
        products_inserted: int,
        products_updated: int,
        products_failed: int,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                _FINISH_RUN_SQL,
                (
                    status,
                    products_discovered,
                    products_inserted,
                    products_updated,
                    products_failed,
                    json.dumps(extra_metadata or {}, ensure_ascii=False),
                    run_id,
                ),
            )
        self._conn.commit()

    def record_error(
        self,
        run_id: int,
        *,
        product_external_id: str | None,
        product_url: str | None,
        stage: str,
        error_code: str | None,
        error_message: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                _INSERT_ERROR_SQL,
                (
                    run_id,
                    product_external_id,
                    product_url,
                    stage,
                    error_code,
                    error_message,
                    json.dumps(raw_payload, ensure_ascii=False, default=str) if raw_payload else None,
                ),
            )
        self._conn.commit()
