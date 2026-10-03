"""Extract schema.org JSON-LD Product / BreadcrumbList blocks.

Confirmed present on live GalaxyStore product pages as of 2026-09
(`<script type="application/ld+json">`), one `Product` block and one
`BreadcrumbList` block per page. This is the most standards-shaped source
available and is preferred first for the fields it reliably carries: the
real manufacturer SKU (`sku`), name, price/currency, and availability.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

_AVAILABILITY_MAP = {
    "https://schema.org/InStock": True,
    "http://schema.org/InStock": True,
    "https://schema.org/OutOfStock": False,
    "http://schema.org/OutOfStock": False,
}


@dataclass
class JsonLdProduct:
    extraction_source: str = "json_ld"
    sku: str | None = None
    name: str | None = None
    brand: str | None = None
    url: str | None = None
    description: str | None = None
    price: float | None = None
    currency: str | None = None
    is_available: bool | None = None
    image: str | None = None
    breadcrumb: list[str] = field(default_factory=list)


def _iter_ld_json_blocks(html: str):
    for m in _LD_JSON_RE.finditer(html):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            yield from obj
        else:
            yield obj


def extract_json_ld(html: str) -> JsonLdProduct:
    """Parse every ld+json block on the page and merge Product/BreadcrumbList data."""
    result = JsonLdProduct()
    for obj in _iter_ld_json_blocks(html):
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("@type")

        if obj_type == "Product":
            result.sku = obj.get("sku") or result.sku
            result.name = obj.get("name") or result.name
            brand = obj.get("brand")
            if isinstance(brand, dict):
                result.brand = brand.get("name") or result.brand
            elif isinstance(brand, str):
                result.brand = brand or result.brand
            result.url = obj.get("url") or result.url
            result.description = obj.get("description") or result.description
            result.image = obj.get("image") or result.image

            offers = obj.get("offers")
            if isinstance(offers, dict):
                price = offers.get("price")
                if price is not None:
                    try:
                        result.price = float(price)
                    except (TypeError, ValueError):
                        pass
                result.currency = offers.get("priceCurrency") or result.currency
                availability = offers.get("availability")
                if availability in _AVAILABILITY_MAP:
                    result.is_available = _AVAILABILITY_MAP[availability]

        elif obj_type == "BreadcrumbList":
            items = obj.get("itemListElement") or []
            crumbs: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not name and isinstance(item.get("item"), dict):
                    name = item["item"].get("name")
                if name:
                    crumbs.append(str(name).strip())
            if crumbs:
                result.breadcrumb = crumbs

    return result
