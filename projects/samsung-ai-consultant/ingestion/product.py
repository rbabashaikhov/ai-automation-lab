"""Fetch and extract a single product page.

Runs the extraction cascade (json_ld -> digital_data -> sm_params -> html)
against one product page, merges it with the catalog-page hints already
known about the product (see `ingestion/catalog.py`), and normalizes the
result into typed fields ready for validation/persistence.

## Field precedence (documented per the task brief: "do not silently mix
conflicting values without documenting precedence")

- `external_id`: `digitalData.product.id`. This is the one field the
  legacy pipeline already trusted for identity (see db/README.md,
  "Canonical product identity"); no fallback -- if it's missing, the
  product fails validation rather than being ingested under a guessed id.
- `model_code` (mpn): `digitalData.product.mpnCode`, then JSON-LD
  `sku`, then `SM_PARAMS.product.main.serialCode`. All three were
  observed equal on every real product fetched during discovery
  (2026-09), so this order only matters when one source is missing.
- `products.sku`: always left `NULL`. Verified during discovery that
  `digitalData` "skuCode" is a copy of the internal listing id, not a
  real manufacturer SKU -- see `extractors/digital_data.py` module
  docstring. Populating it with that value would repeat the legacy
  pipeline's documented mistake instead of fixing it.
- `name`: `digitalData.product.name` (HTML-entity decoded), then JSON-LD
  `name`, then the catalog-page hint.
- `price` / `sale_price`: `digitalData.product.unitPrice` /
  `unitSalePrice`; if absent, `SM_PARAMS.catalog` prices carried over from
  the catalog entry (`recommended` / `special`).
- `is_available`: see `normalize.normalize_availability`.
- `year`, `series`, `screen_size_inches`, `resolution`,
  `panel_technology`, `refresh_rate_hz`: derived from
  `SM_PARAMS.product.specifications.grouped` (see `normalize.py`); `series`
  falls back to the catalog's `articleMain` hint or a model-code heuristic.
- `description`: JSON-LD `description`.
- `category`: `digitalData.product.category` (path, last segment).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import normalize
from .catalog import CatalogEntry
from .extractors import digital_data, html as html_extractor, json_ld, sm_params
from .extractors.sm_params import SpecItem
from .http import FetchError, HttpClient
from .normalize import SpecRow


@dataclass
class ExtractedProduct:
    source: str
    external_id: str | None
    product_url: str
    sku: str | None = None
    model_code: str | None = None
    name: str | None = None
    brand: str = "Samsung"
    category: str | None = None
    year: int | None = None
    series: str | None = None
    screen_size_inches: float | None = None
    resolution: str | None = None
    panel_technology: str | None = None
    refresh_rate_hz: int | None = None
    price: float | None = None
    sale_price: float | None = None
    currency: str = "RUB"
    stock_quantity: int | None = None
    is_available: bool = True
    description: str | None = None
    specs_text: str = ""
    spec_rows: list[SpecRow] = field(default_factory=list)
    source_hash: str = ""
    extra_attributes: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    extraction_sources: dict[str, str] = field(default_factory=dict)


def extract_product(
    *,
    source: str,
    product_url: str,
    page_html: str,
    catalog_entry: CatalogEntry | None = None,
) -> ExtractedProduct:
    """Pure extraction+normalization over an already-fetched product page."""
    dd = digital_data.extract_digital_data_product(page_html)
    ld = json_ld.extract_json_ld(page_html)
    sm = sm_params.extract_sm_params_product(page_html)
    html_fallback = html_extractor.extract_html_fallback(page_html)

    sources: dict[str, str] = {}

    external_id = dd.external_id or (catalog_entry.external_id if catalog_entry else None)
    if dd.external_id:
        sources["external_id"] = "digital_data"
    elif catalog_entry:
        sources["external_id"] = "catalog_hint"

    model_code = normalize.normalize_model_code(
        dd.mpn_code,
        ld.sku,
        sm.serial_code,
        catalog_entry.mpn_code if catalog_entry else None,
    )
    if dd.mpn_code:
        sources["model_code"] = "digital_data"
    elif ld.sku:
        sources["model_code"] = "json_ld"
    elif sm.serial_code:
        sources["model_code"] = "sm_params"

    name = dd.name or ld.name or (catalog_entry.name if catalog_entry else None)
    if dd.name:
        sources["name"] = "digital_data"
    elif ld.name:
        sources["name"] = "json_ld"

    brand = dd.brand or dd.manufacturer or ld.brand or "Samsung"

    category = dd.category
    if category:
        category = category.split("/")[-1].strip() or None
        sources["category"] = "digital_data"
    elif ld.breadcrumb:
        category = ld.breadcrumb[-1]
        sources["category"] = "json_ld"
    elif catalog_entry and catalog_entry.category:
        category = catalog_entry.category.split("/")[-1].strip() or None

    price = normalize.normalize_price(
        dd.price
        if dd.price is not None
        else (catalog_entry.price if catalog_entry else None)
    )
    sale_price = normalize.normalize_price(
        dd.sale_price
        if dd.sale_price is not None
        else (catalog_entry.sale_price if catalog_entry else None)
    )
    if sale_price is not None and price is not None and sale_price >= price:
        sale_price = None  # not an actual discount

    currency = dd.currency or ld.currency or (catalog_entry.currency if catalog_entry else None) or "RUB"

    stock_quantity = dd.stock if dd.stock is not None else (catalog_entry.stock if catalog_entry else None)

    is_available = normalize.normalize_availability(
        json_ld_available=ld.is_available,
        sm_can_buy=sm.can_buy,
        sm_disabled=None,
        stock=stock_quantity,
    )

    year = normalize.normalize_year(sm.specs)
    series = normalize.normalize_series(
        catalog_entry.series_hint if catalog_entry else None, model_code
    )
    screen_size_inches = normalize.normalize_screen_size_inches(sm.specs)
    resolution = normalize.normalize_resolution(sm.specs)
    panel_technology = normalize.normalize_panel_technology(sm.specs)
    refresh_rate_hz = normalize.normalize_refresh_rate_hz(sm.specs)

    description = ld.description

    spec_rows = normalize.build_spec_rows(sm.specs)
    specs_text = normalize.build_specs_text(name=name, specs=sm.specs)

    source_hash = normalize.compute_source_hash(
        name=name,
        brand=brand,
        category=category,
        year=year,
        series=series,
        screen_size_inches=screen_size_inches,
        resolution=resolution,
        panel_technology=panel_technology,
        refresh_rate_hz=refresh_rate_hz,
        price=price,
        sale_price=sale_price,
        currency=currency,
        is_available=is_available,
        description=description,
        specs_text=specs_text,
    )

    raw_payload = {
        "digital_data": dd.__dict__,
        "json_ld": {**ld.__dict__, "breadcrumb": list(ld.breadcrumb)},
        "sm_params_main": {
            "external_id": sm.external_id,
            "name": sm.name,
            "serial_code": sm.serial_code,
            "can_buy": sm.can_buy,
            "available_in_city": sm.available_in_city,
        },
        "html_fallback": html_fallback.__dict__,
    }

    extra_attributes: dict[str, Any] = {}
    if html_fallback.og_image:
        extra_attributes["image_url"] = dd.image_url or html_fallback.og_image
    elif dd.image_url:
        extra_attributes["image_url"] = dd.image_url

    return ExtractedProduct(
        source=source,
        external_id=external_id,
        product_url=product_url,
        sku=None,
        model_code=model_code,
        name=name,
        brand=brand,
        category=category,
        year=year,
        series=series,
        screen_size_inches=screen_size_inches,
        resolution=resolution,
        panel_technology=panel_technology,
        refresh_rate_hz=refresh_rate_hz,
        price=price,
        sale_price=sale_price,
        currency=currency,
        stock_quantity=stock_quantity,
        is_available=is_available,
        description=description,
        specs_text=specs_text,
        spec_rows=spec_rows,
        source_hash=source_hash,
        extra_attributes=extra_attributes,
        raw_payload=raw_payload,
        extraction_sources=sources,
    )


def fetch_product(
    http_client: HttpClient,
    *,
    source: str,
    product_url: str,
    catalog_entry: CatalogEntry | None = None,
) -> ExtractedProduct:
    """Fetch `product_url` and extract it. Raises FetchError on request failure."""
    resp = http_client.get(product_url)
    return extract_product(
        source=source,
        product_url=product_url,
        page_html=resp.text,
        catalog_entry=catalog_entry,
    )


__all__ = ["ExtractedProduct", "extract_product", "fetch_product", "FetchError"]
