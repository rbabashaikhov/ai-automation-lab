"""Plain data model for the fields the indexing pipeline reads.

Deliberately a narrow, independent view of `products` / `product_specs`
(not the ingestion package's `ExtractedProduct` / `SpecRow`) -- indexing
only ever *reads* these two tables (see `repository.py`), so it defines
just the columns it actually consumes rather than importing ingestion's
richer write-side model.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: int
    source: str
    external_id: str
    model_code: str | None
    name: str
    brand: str
    category: str | None
    product_url: str | None
    year: int | None
    series: str | None
    screen_size_inches: float | None
    resolution: str | None
    panel_technology: str | None
    refresh_rate_hz: int | None
    price: float | None
    sale_price: float | None
    currency: str
    is_available: bool
    description: str | None = None


@dataclass(frozen=True)
class Spec:
    spec_group: str
    spec_name: str
    spec_key: str
    spec_value: str
    sort_order: int = 0
