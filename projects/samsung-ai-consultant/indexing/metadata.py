"""Chunk section taxonomy, token counting, and shared metadata construction.

## Section taxonomy

Derived from the actual `spec_group` values observed in the 75-product
production corpus (18 distinct groups -- see
`ingestion/README.md`-adjacent corpus analysis in the Phase 3A report),
folded into 7 semantic sections matching the task brief's suggested
chunk types. The mapping is deliberately explicit and total: every
`spec_group` seen in production maps somewhere, and an unrecognized
future group falls back to `physical_design` (the catchiest "everything
else physical/administrative" bucket) rather than silently disappearing.

## Token counting

Uses `tiktoken`'s `cl100k_base` encoding (the same BPE family
`text-embedding-3-small` uses) when the package is installed locally --
this is a local, offline tokenizer, not a network/embeddings API call.
Falls back to a documented character-based approximation
(`chars / CHARS_PER_TOKEN_APPROX`) if `tiktoken` isn't available, so the
indexing pipeline never hard-depends on it.
"""

from __future__ import annotations

from typing import Any

from .models import Product, Spec

try:
    import tiktoken

    _ENCODING = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - exercised only when tiktoken is absent
    _ENCODING = None

# Empirically, Russian (Cyrillic) text under cl100k_base runs close to
# 2.7-3.0 characters/token (Cyrillic BPE merges are less dense than
# Latin); mixed Russian/Latin/digit product text in this corpus measured
# ~3.2 chars/token against the real tiktoken count. Used only as a
# fallback -- see module docstring.
CHARS_PER_TOKEN_APPROX = 3.2


def approx_token_count(text: str) -> int:
    """Token count for `text`: exact via tiktoken if available, else approximated."""
    if not text:
        return 0
    if _ENCODING is not None:
        return len(_ENCODING.encode(text))
    return max(1, round(len(text) / CHARS_PER_TOKEN_APPROX))


OVERVIEW = "overview"
DISPLAY = "display"
GAMING = "gaming"
AUDIO = "audio"
SMART_FEATURES = "smart_features"
CONNECTIVITY = "connectivity"
PHYSICAL_DESIGN = "physical_design"

SECTION_ORDER = [OVERVIEW, DISPLAY, GAMING, AUDIO, SMART_FEATURES, CONNECTIVITY, PHYSICAL_DESIGN]

SECTION_LABELS = {
    OVERVIEW: "Основные характеристики",
    DISPLAY: "Экран и изображение",
    GAMING: "Игровые функции",
    AUDIO: "Звук",
    SMART_FEATURES: "Smart TV и функции",
    CONNECTIVITY: "Подключение",
    PHYSICAL_DESIGN: "Конструкция и комплектация",
}

# spec_group -> section. Every group observed in the 75-product corpus is
# listed explicitly (see module docstring); "Основные характеристики" is
# folded into OVERVIEW since its content (screen tech/processor/year) is
# already restated from typed columns in the overview chunk's header.
SPEC_GROUP_TO_SECTION: dict[str, str] = {
    "Основные характеристики": OVERVIEW,
    "Экран и разрешение": DISPLAY,
    "Изображение": DISPLAY,
    "Настройка изображения": DISPLAY,
    "Проекционная система": DISPLAY,
    "Игровой режим": GAMING,
    "Звук": AUDIO,
    "Поддержка Smart TV": SMART_FEATURES,
    "Функции": SMART_FEATURES,
    "Поддерживаемые форматы": SMART_FEATURES,
    "Интерфейсы": CONNECTIVITY,
    "Беспроводная связь": CONNECTIVITY,
    "Технологии приема сигнала": CONNECTIVITY,
    "Корпус": PHYSICAL_DESIGN,
    "Размеры и вес": PHYSICAL_DESIGN,
    "Заводские данные": PHYSICAL_DESIGN,
    "Комплектация": PHYSICAL_DESIGN,
    "Электропитание": PHYSICAL_DESIGN,
}


def section_for_spec_group(spec_group: str) -> str:
    return SPEC_GROUP_TO_SECTION.get(spec_group, PHYSICAL_DESIGN)


def build_structured_metadata(product: Product) -> dict[str, Any]:
    """Structured, filterable fields shared by a product's document and every
    one of its chunks -- the typed attributes `match_product_chunks()`
    filters on (see db/migrations/005_functions.sql), kept here so a
    chunk is self-describing without a join back to `products`.
    Deliberately excludes `raw_payload`/large blobs -- see module note in
    builder.py "Fields intentionally excluded".
    """
    return {
        "product_id": product.id,
        "source": product.source,
        "external_id": product.external_id,
        "model_code": product.model_code,
        "product_url": product.product_url,
        "year": product.year,
        "series": product.series,
        "screen_size_inches": product.screen_size_inches,
        "resolution": product.resolution,
        "panel_technology": product.panel_technology,
        "refresh_rate_hz": product.refresh_rate_hz,
        "price": product.price,
        "currency": product.currency,
        "is_available": product.is_available,
    }


def group_specs_by_section(specs: list[Spec]) -> dict[str, list[Spec]]:
    """Group specs by target section, preserving each group's original
    `sort_order` within its section and iterating sections in
    `SECTION_ORDER`."""
    by_section: dict[str, list[Spec]] = {section: [] for section in SECTION_ORDER}
    for spec in specs:
        by_section[section_for_spec_group(spec.spec_group)].append(spec)
    for section_specs in by_section.values():
        section_specs.sort(key=lambda s: s.sort_order)
    return by_section
