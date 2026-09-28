"""Deterministic document construction for one product.

## What goes in

A typed-attribute header (name, model, series, year, screen size,
resolution, panel technology, refresh rate, price, availability,
category) followed by every `product_specs` row, grouped by its original
`spec_group`, as `Name: value` lines. No LLM is involved -- this is plain
string formatting over already-structured data (`products` typed columns
+ `product_specs`), matching the task's "deterministic code, not
generated content" requirement.

## Fields intentionally excluded (and why)

- **`products.description`.** Verified against the real corpus (all 75
  rows): this field is JSON-LD storefront SEO/marketing copy scraped
  verbatim in Phase 2 -- e.g. `"📱Купить Телевизор Samsung 50&quot;
  ... с доставкой в городе Москве и всей России 🚚 ... ✅Гарантия.
  ✅Рассрочка или кредит."` It contains emojis, an undecoded HTML entity
  (`&quot;`), and generic storefront calls-to-action repeated near
  verbatim across every product -- zero retrieval-differentiating
  product-characteristic content, and exactly the kind of "invented
  marketing claim" text the task brief says to keep out of RAG content.
  Excluded entirely, not filtered/rewritten.
- **Five `product_specs` rows whose values are already restated from
  typed columns in the header**: "Год выпуска" (`year`), "Диагональ,
  дюйм" (`screen_size_inches`), "Разрешение" (`resolution`), "Технология
  экрана" (`panel_technology`), "Частота обновления, Гц"
  (`refresh_rate_hz`). Keeping both would duplicate the same fact twice
  in the same document/chunk -- excluded from the spec listing body only;
  the underlying characteristic is still present, once, in the header.

Nothing else is excluded. Every other spec observed in the corpus --
including ones that happen to have the same value across all 75 current
products (e.g. "Smart TV: Да", "Поддержка Wi-Fi: Да") -- is a genuine,
real product characteristic that could differentiate a future catalog
addition, so it is kept per the task brief's "do not remove real
characteristics merely because they appear unimportant."
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .metadata import SECTION_ORDER, build_structured_metadata, group_specs_by_section
from .models import Product, Spec

# spec_name values whose fact is already stated from a typed column in
# the header -- see module docstring "Fields intentionally excluded".
HEADER_DUPLICATE_SPEC_NAMES = frozenset(
    {
        "Год выпуска",
        "Диагональ, дюйм",
        "Разрешение",
        "Технология экрана",
        "Частота обновления, Гц",
    }
)

DOCUMENT_TYPE_PRODUCT_OVERVIEW = "product_overview"


@dataclass
class DocumentDraft:
    document_type: str
    title: str
    content: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _format_price(product: Product) -> str:
    if product.price is None:
        return "цена не указана"
    if product.sale_price is not None and product.sale_price < product.price:
        return f"{product.sale_price:.0f} {product.currency} (без скидки {product.price:.0f} {product.currency})"
    return f"{product.price:.0f} {product.currency}"


def build_header_lines(product: Product) -> list[str]:
    lines = [f"Samsung {product.name}"]
    if product.model_code:
        lines.append(f"Модель: {product.model_code}")
    if product.series:
        lines.append(f"Серия: {product.series}")
    if product.category:
        lines.append(f"Категория: {product.category}")
    if product.year:
        lines.append(f"Год: {product.year}")
    if product.screen_size_inches:
        lines.append(f'Диагональ: {product.screen_size_inches:g}"')
    if product.resolution:
        lines.append(f"Разрешение: {product.resolution}")
    if product.panel_technology:
        lines.append(f"Тип экрана: {product.panel_technology}")
    if product.refresh_rate_hz:
        lines.append(f"Частота обновления: {product.refresh_rate_hz} Гц")
    lines.append(f"Цена: {_format_price(product)}")
    lines.append(f"Наличие: {'в наличии' if product.is_available else 'нет в наличии'}")
    return lines


def _spec_lines(specs: list[Spec]) -> list[str]:
    return [
        f"{spec.spec_name}: {spec.spec_value}"
        for spec in specs
        if spec.spec_name not in HEADER_DUPLICATE_SPEC_NAMES
    ]


def build_document_content(product: Product, specs: list[Spec]) -> str:
    lines = list(build_header_lines(product))

    by_section = group_specs_by_section(specs)
    for section in SECTION_ORDER:
        section_specs = by_section[section]
        spec_lines = _spec_lines(section_specs)
        if not spec_lines:
            continue
        lines.append("")
        lines.append(f"{_group_label(section_specs)}:")
        lines.extend(f"  {line}" for line in spec_lines)

    return "\n".join(lines).strip()


def _group_label(section_specs: list[Spec]) -> str:
    """Use the original (Russian) spec_group name(s) as the sub-heading,
    joined if a section merges more than one source group -- keeps the
    document traceable to the real source taxonomy rather than only the
    7-way section simplification (see metadata.py)."""
    seen: list[str] = []
    for spec in section_specs:
        if spec.spec_group not in seen:
            seen.append(spec.spec_group)
    return " / ".join(seen)


def build_document(product: Product, specs: list[Spec]) -> DocumentDraft:
    content = build_document_content(product, specs)
    metadata = build_structured_metadata(product)
    metadata["document_type"] = DOCUMENT_TYPE_PRODUCT_OVERVIEW
    metadata["spec_count"] = len(specs)
    return DocumentDraft(
        document_type=DOCUMENT_TYPE_PRODUCT_OVERVIEW,
        title=f"Samsung {product.name}",
        content=content,
        content_hash=compute_content_hash(content),
        metadata=metadata,
    )
