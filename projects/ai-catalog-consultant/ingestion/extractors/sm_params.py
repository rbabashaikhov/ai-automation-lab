"""Extract SM_PARAMS.catalog (listing pages) and SM_PARAMS.product (detail pages).

Confirmed present on live GalaxyStore pages as of 2026-09, but in a
different shape than the legacy `parsing.sanitized.json` Code node
assumed: legacy looked for a single `window.SM_PARAMS = {..., catalog:
{...}, ...}` object literal and tried to carve `catalog` back out of it
with a regex. The live site instead assigns each section as its own
top-level statement -- `SM_PARAMS.catalog = {...};`,
`SM_PARAMS.product = {...};`, etc. -- which is actually simpler to target
directly.

`SM_PARAMS.product.specifications.grouped` is the richest structured
source on the page: a list of spec groups, each with named items and
string values, e.g. `{"name": "Экран и разрешение", "items": [{"name":
"Диагональ, дюйм", "values": ["50"]}, ...]}`. This is the primary source
for typed columns (screen size, resolution, panel technology, refresh
rate, year) and for `product_specs` rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import extract_balanced_json

_CATALOG_RE = r"SM_PARAMS\.catalog\s*=\s*\{"
_PRODUCT_RE = r"SM_PARAMS\.product\s*=\s*\{"


@dataclass
class SpecItem:
    spec_group: str
    spec_name: str
    values: list[str]
    sort_order: int = 0


@dataclass
class SmParamsCatalogItem:
    extraction_source: str = "sm_params"
    external_id: str | None = None
    name: str | None = None
    detail_url: str | None = None
    series_hint: str | None = None  # SM_PARAMS.catalog "articleMain", e.g. "M70"
    price_special: float | None = None
    price_recommended: float | None = None
    can_buy: bool | None = None
    disabled: bool | None = None
    available_in_city: bool | None = None
    images: list[str] = field(default_factory=list)


@dataclass
class SmParamsProduct:
    extraction_source: str = "sm_params"
    external_id: str | None = None
    name: str | None = None
    serial_code: str | None = None  # SM_PARAMS.product.main "serialCode" -- matches mpnCode/JSON-LD sku
    can_buy: bool | None = None
    available_in_city: bool | None = None
    specs: list[SpecItem] = field(default_factory=list)


def _load_balanced(html: str, pattern: str) -> dict | None:
    chunk = extract_balanced_json(html, pattern)
    if chunk is None:
        return None
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        return None


def extract_sm_params_catalog(html: str) -> list[SmParamsCatalogItem]:
    """Parse `SM_PARAMS.catalog.items` from a catalog listing page."""
    obj = _load_balanced(html, _CATALOG_RE)
    if not obj:
        return []
    items = obj.get("items") or []
    result: list[SmParamsCatalogItem] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        prices = it.get("prices") or {}
        result.append(
            SmParamsCatalogItem(
                external_id=str(it["id"]) if it.get("id") is not None else None,
                name=it.get("name"),
                detail_url=it.get("detailUrl"),
                series_hint=it.get("articleMain") or None,
                price_special=_to_number(prices.get("special")),
                price_recommended=_to_number(prices.get("recommended")),
                can_buy=it.get("canBuy"),
                disabled=it.get("disabled"),
                available_in_city=it.get("availableInCity"),
                images=list(it.get("images") or []),
            )
        )
    return result


def extract_sm_params_product(html: str) -> SmParamsProduct:
    """Parse `SM_PARAMS.product` from a product detail page, including specs."""
    obj = _load_balanced(html, _PRODUCT_RE)
    if not obj:
        return SmParamsProduct()

    main = obj.get("main") or {}
    specs: list[SpecItem] = []
    grouped = ((obj.get("specifications") or {}).get("grouped")) or []
    sort_order = 0
    for group in grouped:
        if not isinstance(group, dict):
            continue
        group_name = group.get("name") or ""
        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue
            values = item.get("values") or []
            specs.append(
                SpecItem(
                    spec_group=group_name,
                    spec_name=item.get("name") or "",
                    values=[str(v) for v in values],
                    sort_order=sort_order,
                )
            )
            sort_order += 1

    return SmParamsProduct(
        external_id=str(main["id"]) if main.get("id") is not None else None,
        name=main.get("name"),
        serial_code=main.get("serialCode"),
        can_buy=main.get("canBuy"),
        available_in_city=main.get("availableInCity"),
        specs=specs,
    )


def _to_number(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
