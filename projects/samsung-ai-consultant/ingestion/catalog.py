"""Catalog discovery: paginate a GalaxyStore listing and yield product entries.

Pagination is driven primarily by `digitalData.listing.currentPage` /
`pagesCount` (clean integers, confirmed present on every catalog page as
of 2026-09) rather than only the `<link rel="next">` tag, though both are
checked and cross-referenced -- see `_resolve_next_url`. A page is only
followed once: visited URLs are tracked to prevent infinite pagination
loops (e.g. a misbehaving `next` link pointing back at an earlier page),
and `max_pages` caps how many pages are fetched regardless.

Per the task brief, pagination metadata is represented separately from
products (`CatalogPage.pages_count` / `.has_next`), never injected into
the product list as a synthetic `_meta` row the way the legacy workflow
did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .extractors import digital_data, html as html_extractor, sm_params
from .http import FetchError, HttpClient

logger = logging.getLogger(__name__)


@dataclass
class CatalogEntry:
    external_id: str
    product_url: str
    mpn_code: str | None = None
    name: str | None = None
    series_hint: str | None = None
    category: str | None = None
    price: float | None = None
    sale_price: float | None = None
    currency: str | None = None
    stock: int | None = None
    available_hint: bool | None = None


@dataclass
class CatalogPage:
    url: str
    entries: list[CatalogEntry] = field(default_factory=list)
    current_page: int | None = None
    pages_count: int | None = None
    next_url: str | None = None


def _resolve_next_url(page_url: str, dd_listing, html_fallback) -> str | None:
    """Prefer digitalData's page counters; fall back to <link rel="next">."""
    if dd_listing.current_page is not None and dd_listing.pages_count is not None:
        if dd_listing.current_page >= dd_listing.pages_count:
            return None
    if html_fallback.next_page_url:
        return urljoin(page_url, html_fallback.next_page_url)
    return None


def parse_catalog_page(url: str, page_html: str) -> CatalogPage:
    """Extract product entries + pagination info from one already-fetched page."""
    dd_listing = digital_data.extract_digital_data_listing(page_html)
    sm_items = sm_params.extract_sm_params_catalog(page_html)
    html_fallback = html_extractor.extract_html_fallback(page_html)

    sm_by_id = {item.external_id: item for item in sm_items if item.external_id}

    entries: list[CatalogEntry] = []
    seen_ids: set[str] = set()
    for dd_item in dd_listing.items:
        if not dd_item.external_id or not dd_item.product_url:
            logger.warning("Skipping catalog item with missing id/url on %s", url)
            continue
        if dd_item.external_id in seen_ids:
            continue
        seen_ids.add(dd_item.external_id)

        sm_item = sm_by_id.get(dd_item.external_id)
        entries.append(
            CatalogEntry(
                external_id=dd_item.external_id,
                product_url=dd_item.product_url,
                mpn_code=dd_item.mpn_code,
                name=dd_item.name,
                series_hint=sm_item.series_hint if sm_item else None,
                category=dd_item.category,
                price=dd_item.price,
                sale_price=dd_item.sale_price,
                currency=dd_item.currency,
                stock=dd_item.stock,
                available_hint=(
                    (sm_item.can_buy and not sm_item.disabled) if sm_item else None
                ),
            )
        )

    next_url = _resolve_next_url(url, dd_listing, html_fallback)

    return CatalogPage(
        url=url,
        entries=entries,
        current_page=dd_listing.current_page,
        pages_count=dd_listing.pages_count,
        next_url=next_url,
    )


def discover_catalog(
    http_client: HttpClient,
    start_url: str,
    max_pages: int | None = None,
) -> tuple[list[CatalogEntry], list[CatalogPage]]:
    """Walk catalog pagination from `start_url`, returning deduplicated entries.

    Returns `(entries, pages)`: `entries` is deduplicated by
    `product_url` across all visited pages (a product occasionally
    reappearing on two pages due to sort drift is not treated as an
    error); `pages` is the per-page pagination trail, for diagnostics/
    `ingestion_runs.metadata`.
    """
    pages: list[CatalogPage] = []
    entries_by_url: dict[str, CatalogEntry] = {}
    visited: set[str] = set()

    next_url: str | None = start_url
    while next_url:
        if next_url in visited:
            logger.warning("Pagination loop detected at %s; stopping", next_url)
            break
        if max_pages is not None and len(pages) >= max_pages:
            logger.info("Reached max_pages=%s; stopping catalog discovery", max_pages)
            break

        visited.add(next_url)
        try:
            resp = http_client.get(next_url)
        except FetchError:
            logger.exception("Failed to fetch catalog page %s", next_url)
            break

        page = parse_catalog_page(next_url, resp.text)
        pages.append(page)
        for entry in page.entries:
            entries_by_url[entry.product_url] = entry

        next_url = page.next_url

    return list(entries_by_url.values()), pages
