"""Orchestrates discover -> fetch -> extract -> normalize -> validate -> persist.

One bad product must not abort the whole run: every per-product step is
wrapped so a fetch failure or validation failure is recorded as an
`ingestion_errors` row (or, in dry-run, just reported) and the run moves
on to the next product.

## Dry-run and `ingestion_runs`

`dry_run=True` performs extraction, normalization, and validation exactly
as a real run does, but **never writes to PostgreSQL at all** -- no
`products`/`product_specs` writes, and also **no `ingestion_runs` /
`ingestion_errors` row**, since those tables record what an ingestion
*execution* did, and a dry-run doesn't execute one. This is a deliberate,
documented choice (the task brief asks for it to be explicit): a dry-run
summary is only ever printed to the CLI, never persisted anywhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .catalog import CatalogEntry, CatalogPage, discover_catalog
from .http import FetchError, HttpClient
from .product import ExtractedProduct, fetch_product
from .repository import IngestionRunRepository, ProductRepository
from .validate import ValidationResult, validate_product

logger = logging.getLogger(__name__)


@dataclass
class ProductOutcome:
    external_id: str | None
    product_url: str
    status: str  # "inserted" | "updated" | "failed" | "invalid" | "dry_run_ok"
    product: ExtractedProduct | None = None
    validation: ValidationResult | None = None
    error: str | None = None


@dataclass
class RunSummary:
    run_id: int | None
    dry_run: bool
    source: str
    source_url: str
    pages: list[CatalogPage] = field(default_factory=list)
    outcomes: list[ProductOutcome] = field(default_factory=list)
    status: str = "running"

    @property
    def products_discovered(self) -> int:
        return len(self.outcomes)

    @property
    def products_inserted(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "inserted")

    @property
    def products_updated(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "updated")

    @property
    def products_failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("failed", "invalid"))


def _process_one(
    http_client: HttpClient,
    *,
    source: str,
    entry: CatalogEntry,
    dry_run: bool,
    product_repo: ProductRepository | None,
    run_repo: IngestionRunRepository | None,
    run_id: int | None,
) -> ProductOutcome:
    try:
        product = fetch_product(
            http_client, source=source, product_url=entry.product_url, catalog_entry=entry
        )
    except FetchError as exc:
        logger.warning("Fetch failed for %s: %s", entry.product_url, exc)
        if run_repo and run_id is not None:
            run_repo.record_error(
                run_id,
                product_external_id=entry.external_id,
                product_url=entry.product_url,
                stage="fetch",
                error_code=str(exc.status_code) if exc.status_code else "fetch_error",
                error_message=str(exc),
            )
        return ProductOutcome(
            external_id=entry.external_id,
            product_url=entry.product_url,
            status="failed",
            error=str(exc),
        )

    validation = validate_product(product)
    if not validation.is_valid:
        message = "; ".join(f"{e.code}: {e.message}" for e in validation.errors)
        logger.warning("Validation failed for %s: %s", entry.product_url, message)
        if run_repo and run_id is not None:
            run_repo.record_error(
                run_id,
                product_external_id=product.external_id,
                product_url=entry.product_url,
                stage="validate",
                error_code=validation.errors[0].code,
                error_message=message,
                raw_payload=product.raw_payload,
            )
        return ProductOutcome(
            external_id=entry.external_id,
            product_url=entry.product_url,
            status="invalid",
            product=product,
            validation=validation,
            error=message,
        )

    if dry_run:
        return ProductOutcome(
            external_id=entry.external_id,
            product_url=entry.product_url,
            status="dry_run_ok",
            product=product,
            validation=validation,
        )

    assert product_repo is not None
    try:
        result = product_repo.save_product(product)
    except Exception as exc:  # noqa: BLE001 - persistence failures are per-product, not fatal
        logger.exception("Persistence failed for %s", entry.product_url)
        if run_repo and run_id is not None:
            run_repo.record_error(
                run_id,
                product_external_id=product.external_id,
                product_url=entry.product_url,
                stage="persist",
                error_code="db_error",
                error_message=str(exc),
            )
        return ProductOutcome(
            external_id=entry.external_id,
            product_url=entry.product_url,
            status="failed",
            product=product,
            error=str(exc),
        )

    return ProductOutcome(
        external_id=entry.external_id,
        product_url=entry.product_url,
        status="inserted" if result.inserted else "updated",
        product=product,
    )


def run_ingestion(
    http_client: HttpClient,
    *,
    source: str,
    source_url: str,
    max_pages: int | None = None,
    limit_products: int | None = None,
    dry_run: bool = False,
    product_repo: ProductRepository | None = None,
    run_repo: IngestionRunRepository | None = None,
) -> RunSummary:
    if not dry_run and (product_repo is None or run_repo is None):
        raise ValueError("product_repo and run_repo are required when dry_run=False")

    entries, pages = discover_catalog(http_client, source_url, max_pages=max_pages)
    if limit_products is not None:
        entries = entries[:limit_products]

    run_id: int | None = None
    if not dry_run:
        assert run_repo is not None
        run_id = run_repo.create_run(
            source=source,
            source_url=source_url,
            metadata={
                "max_pages": max_pages,
                "limit_products": limit_products,
                "pages_visited": len(pages),
            },
        )

    summary = RunSummary(run_id=run_id, dry_run=dry_run, source=source, source_url=source_url, pages=pages)

    for entry in entries:
        outcome = _process_one(
            http_client,
            source=source,
            entry=entry,
            dry_run=dry_run,
            product_repo=product_repo,
            run_repo=run_repo,
            run_id=run_id,
        )
        summary.outcomes.append(outcome)

    if summary.products_failed == 0:
        summary.status = "success"
    elif summary.products_failed < summary.products_discovered:
        summary.status = "partial"
    else:
        summary.status = "failed" if summary.products_discovered > 0 else "success"

    if not dry_run:
        assert run_repo is not None and run_id is not None
        run_repo.finish_run(
            run_id,
            status=summary.status,
            products_discovered=summary.products_discovered,
            products_inserted=summary.products_inserted,
            products_updated=summary.products_updated,
            products_failed=summary.products_failed,
            extra_metadata={"pages_visited": len(pages)},
        )

    return summary
