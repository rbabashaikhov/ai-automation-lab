"""Extract window.digitalData from catalog listing and product pages.

Confirmed present on live GalaxyStore pages as of 2026-09. This is the
most structurally reliable source for identity and pricing fields, and is
used as the primary source for `external_id`.

Important, verified finding (matches the Phase 1 legacy-discovery note in
db/README.md "Canonical product identity"): `digitalData` items carry both
`id` and `skuCode`, and on the live site these are always equal — e.g.
`{"id": "3965052", "skuCode": "3965052", ...}`. `skuCode` is therefore
**not** a real manufacturer SKU, just a copy of the internal listing id.
The real manufacturer model code lives in `mpnCode` (e.g.
`UE50M70HAUXPY`), which also matches the JSON-LD `sku` field on the
product page. This module surfaces both, undisguised, so callers can make
an informed precedence decision instead of inheriting the legacy
conflation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import decode_entities, extract_balanced_json

_DIGITAL_DATA_RE = r"window\.digitalData\s*=\s*\{"


@dataclass
class DigitalDataCatalogItem:
    extraction_source: str = "digital_data"
    external_id: str | None = None
    internal_sku_code: str | None = None  # digitalData "skuCode" -- NOT a real SKU, see module docstring
    mpn_code: str | None = None
    name: str | None = None
    brand: str | None = None
    category: str | None = None
    product_url: str | None = None
    price: float | None = None
    sale_price: float | None = None
    currency: str | None = None
    stock: int | None = None
    image_url: str | None = None


@dataclass
class DigitalDataListing:
    items: list[DigitalDataCatalogItem] = field(default_factory=list)
    result_count: int | None = None
    pages_count: int | None = None
    current_page: int | None = None


@dataclass
class DigitalDataProduct:
    extraction_source: str = "digital_data"
    external_id: str | None = None
    internal_sku_code: str | None = None
    mpn_code: str | None = None
    mpn_group_code: str | None = None
    name: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    product_url: str | None = None
    price: float | None = None
    sale_price: float | None = None
    currency: str | None = None
    stock: int | None = None
    image_url: str | None = None


def _load_digital_data(html: str) -> dict | None:
    chunk = extract_balanced_json(html, _DIGITAL_DATA_RE)
    if chunk is None:
        return None
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        return None


def _to_number(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_digital_data_listing(html: str) -> DigitalDataListing:
    """Parse `window.digitalData.listing` from a catalog page."""
    dd = _load_digital_data(html)
    if not dd:
        return DigitalDataListing()

    listing = dd.get("listing") or {}
    raw_items = listing.get("items") or []
    items: list[DigitalDataCatalogItem] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        items.append(
            DigitalDataCatalogItem(
                external_id=_as_str(it.get("id")),
                internal_sku_code=_as_str(it.get("skuCode")),
                mpn_code=it.get("mpnCode"),
                name=decode_entities(it.get("name")),
                brand=it.get("brand"),
                category=it.get("category"),
                product_url=it.get("url"),
                price=_to_number(it.get("unitPrice")),
                sale_price=_to_number(it.get("unitSalePrice")),
                currency=it.get("currency"),
                stock=_as_int(it.get("stock")),
                image_url=it.get("imageUrl"),
            )
        )

    return DigitalDataListing(
        items=items,
        result_count=_as_int(listing.get("resultCount")),
        pages_count=_as_int(listing.get("pagesCount")),
        current_page=_as_int(listing.get("currentPage")),
    )


def extract_digital_data_product(html: str) -> DigitalDataProduct:
    """Parse `window.digitalData.product` from a product detail page."""
    dd = _load_digital_data(html)
    if not dd:
        return DigitalDataProduct()

    p = dd.get("product") or {}
    return DigitalDataProduct(
        external_id=_as_str(p.get("id")),
        internal_sku_code=_as_str(p.get("skuCode")),
        mpn_code=p.get("mpnCode"),
        mpn_group_code=p.get("mpnGroupCode"),
        name=decode_entities(p.get("name")),
        brand=p.get("brand"),
        manufacturer=p.get("manufacturer"),
        category=p.get("category"),
        product_url=p.get("url"),
        price=_to_number(p.get("unitPrice")),
        sale_price=_to_number(p.get("unitSalePrice")),
        currency=p.get("currency"),
        stock=_as_int(p.get("stock")),
        image_url=p.get("imageUrl"),
    )


def _as_str(value) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
