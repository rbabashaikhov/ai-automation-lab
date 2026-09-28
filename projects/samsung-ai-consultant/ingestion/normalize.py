"""Deterministic normalization of raw extracted fields into typed columns.

Every function here is a pure, deterministic mapping from raw scraped
strings to a typed value or `None`. None of them guess: if the source
data doesn't contain a confident answer, the function returns `None`
rather than inferring one (per the task brief, "Do NOT hallucinate or
derive uncertain values").

The GalaxyStore `SM_PARAMS.product.specifications.grouped` source (see
`ingestion/extractors/sm_params.py`) already gives most target fields as
clean, near-final strings -- e.g. "Диагональ, дюйм": ["50"], "Частота
обновления, Гц": ["60"], "Год выпуска": ["2026"] -- so normalization here
is mostly about locating the right spec by name and doing light type
coercion, not free-text unit parsing. `normalize_resolution` is the one
exception: it does parse an explicit pixel-dimension pattern out of a
longer label, and falls back to a small, fixed table of unambiguous
industry-standard resolution labels (documented below) only when no
explicit dimensions are present.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .extractors.sm_params import SpecItem

# Spec names as they currently appear on GalaxyStore product pages
# (Russian, exact match against SpecItem.spec_name). Kept as a small,
# explicit table rather than fuzzy matching, so a site wording change
# fails loudly (produces NULL, not a wrong value) instead of silently.
SPEC_NAME_SCREEN_SIZE = "Диагональ, дюйм"
SPEC_NAME_RESOLUTION = "Разрешение"
SPEC_NAME_PANEL_TECHNOLOGY = "Технология экрана"
SPEC_NAME_REFRESH_RATE = "Частота обновления, Гц"
SPEC_NAME_YEAR = "Год выпуска"

# Fixed, industry-standard resolution labels -> pixel dimensions. Used
# only as a fallback when the spec value has no explicit "WxH" pattern.
# These are unambiguous TV industry definitions, not per-product guesses.
_KNOWN_RESOLUTION_LABELS = {
    "8k": "7680x4320",
    "4k ultra hd": "3840x2160",
    "4k": "3840x2160",
    "full hd": "1920x1080",
    "fhd": "1920x1080",
    "hd ready": "1280x720",
    "hd": "1280x720",
}

_RESOLUTION_DIMENSIONS_RE = re.compile(r"(\d{3,4})\s*[xх×]\s*(\d{3,4})")


def _find_spec_value(specs: list[SpecItem], spec_name: str) -> str | None:
    for spec in specs:
        if spec.spec_name == spec_name and spec.values:
            return spec.values[0]
    return None


def normalize_screen_size_inches(specs: list[SpecItem]) -> float | None:
    raw = _find_spec_value(specs, SPEC_NAME_SCREEN_SIZE)
    if raw is None:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)", raw)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def normalize_resolution(specs: list[SpecItem]) -> str | None:
    raw = _find_spec_value(specs, SPEC_NAME_RESOLUTION)
    if raw is None:
        return None
    dims = _RESOLUTION_DIMENSIONS_RE.search(raw)
    if dims:
        return f"{dims.group(1)}x{dims.group(2)}"
    return _KNOWN_RESOLUTION_LABELS.get(raw.strip().lower())


def normalize_panel_technology(specs: list[SpecItem]) -> str | None:
    raw = _find_spec_value(specs, SPEC_NAME_PANEL_TECHNOLOGY)
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def normalize_refresh_rate_hz(specs: list[SpecItem]) -> int | None:
    raw = _find_spec_value(specs, SPEC_NAME_REFRESH_RATE)
    if raw is None:
        return None
    match = re.search(r"(\d+)", raw)
    if not match:
        return None
    return int(match.group(1))


def normalize_year(specs: list[SpecItem]) -> int | None:
    raw = _find_spec_value(specs, SPEC_NAME_YEAR)
    if raw is None:
        return None
    match = re.search(r"(\d{4})", raw)
    if not match:
        return None
    return int(match.group(1))


_MODEL_CODE_SERIES_RE = re.compile(r"^[A-Z]{2}\d{2,3}([A-Z]{1,3}\d{1,3})")


def normalize_series(series_hint: str | None, model_code: str | None) -> str | None:
    """Prefer the catalog's explicit `articleMain` hint (e.g. "M70").

    Falls back to parsing the series token out of the model code (e.g.
    "UE50M70HAUXPY" -> "M70") using the observed Samsung TV model-code
    convention {panel prefix}{size}{series}{suffix}. This is a documented,
    revisitable heuristic (see README "Known limitations"), not verified
    against Samsung's full official model-code spec.
    """
    if series_hint:
        return series_hint.strip()
    if model_code:
        match = _MODEL_CODE_SERIES_RE.match(model_code.strip().upper())
        if match:
            return match.group(1)
    return None


def normalize_model_code(*candidates: str | None) -> str | None:
    """First non-empty candidate, uppercased and trimmed.

    Callers pass candidates in preference order (e.g. digitalData mpnCode,
    then JSON-LD sku, then SM_PARAMS serialCode) -- see product.py for the
    documented precedence.
    """
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip().upper()
    return None


def normalize_price(value: float | None) -> float | None:
    if value is None or value < 0:
        return None
    return round(value, 2)


def normalize_availability(
    *,
    json_ld_available: bool | None,
    sm_can_buy: bool | None,
    sm_disabled: bool | None,
    stock: int | None,
) -> bool:
    """Combine availability signals with documented precedence.

    1. JSON-LD `offers.availability` (explicit schema.org signal) if present.
    2. SM_PARAMS `canBuy and not disabled` if present.
    3. `stock > 0` as a last resort.
    4. Default to True (assume available) only if every signal is absent --
       conservative default matching "don't invent unavailability."
    """
    if json_ld_available is not None:
        return json_ld_available
    if sm_can_buy is not None:
        return bool(sm_can_buy) and not bool(sm_disabled)
    if stock is not None:
        return stock > 0
    return True


@dataclass
class SpecRow:
    spec_group: str
    spec_name: str
    spec_key: str
    spec_value: str
    normalized_value: str | None
    unit: str | None
    sort_order: int


_UNIT_SUFFIXES = (
    (", дюйм", "inch"),
    (", Гц", "Hz"),
    (", мм", "mm"),
    (", см", "cm"),
    (", кг", "kg"),
    (", Вт", "W"),
)

_SLUG_TRANSLIT = str.maketrans(
    "абвгдежзийклмнопрстуфхцчшщъыьэюя",
    "abvgdejzijklmnoprstufhccss_y_eua",
)


def _slugify(spec_name: str) -> str:
    base = spec_name.lower()
    for suffix, _ in _UNIT_SUFFIXES:
        base = base.replace(suffix.lower(), "")
    base = base.translate(_SLUG_TRANSLIT)
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return base or "spec"


def build_spec_rows(specs: list[SpecItem]) -> list[SpecRow]:
    """Flatten SM_PARAMS grouped specs into `product_specs` rows.

    `spec_key` is a deterministic slug derived from `spec_name` (see
    `_slugify`); collisions within one product are disambiguated with a
    numeric suffix so `UNIQUE (product_id, spec_key)` never fails on
    legitimately-repeated spec labels.
    """
    rows: list[SpecRow] = []
    seen_keys: dict[str, int] = {}
    for spec in specs:
        unit = None
        for suffix, unit_label in _UNIT_SUFFIXES:
            if spec.spec_name.endswith(suffix):
                unit = unit_label
                break

        base_key = _slugify(spec.spec_name)
        count = seen_keys.get(base_key, 0)
        seen_keys[base_key] = count + 1
        spec_key = base_key if count == 0 else f"{base_key}_{count + 1}"

        value_text = "; ".join(spec.values)
        rows.append(
            SpecRow(
                spec_group=spec.spec_group,
                spec_name=spec.spec_name,
                spec_key=spec_key,
                spec_value=value_text,
                normalized_value=value_text if len(spec.values) == 1 else None,
                unit=unit,
                sort_order=spec.sort_order,
            )
        )
    return rows


def build_specs_text(
    *,
    name: str | None,
    specs: list[SpecItem],
) -> str:
    """Build a clean, RAG-friendly text block from name + grouped specs.

    Deliberately excludes HTML/navigation noise -- it is built entirely
    from the already-structured `SM_PARAMS.product.specifications.grouped`
    data, never from raw page markup.
    """
    lines: list[str] = []
    if name:
        lines.append(name.strip())

    current_group: str | None = None
    for spec in specs:
        if spec.spec_group != current_group:
            current_group = spec.spec_group
            lines.append(f"\n{current_group}:")
        values = ", ".join(spec.values)
        lines.append(f"  {spec.spec_name}: {values}")

    return "\n".join(lines).strip()


def compute_source_hash(
    *,
    name: str | None,
    brand: str | None,
    category: str | None,
    year: int | None,
    series: str | None,
    screen_size_inches: float | None,
    resolution: str | None,
    panel_technology: str | None,
    refresh_rate_hz: int | None,
    price: float | None,
    sale_price: float | None,
    currency: str | None,
    is_available: bool,
    description: str | None,
    specs_text: str,
) -> str:
    """SHA-256 over a canonical JSON of meaningful, content-bearing fields.

    Deliberately excludes anything timestamp-like or unstable-per-request
    (stock exact count, review counts, image CDN cache paths, page
    generation timestamps) so re-scraping an unchanged product yields an
    identical hash -- the whole point is to let a future Phase 3 skip
    re-embedding/re-indexing products that haven't meaningfully changed.
    """
    payload = {
        "name": name,
        "brand": brand,
        "category": category,
        "year": year,
        "series": series,
        "screen_size_inches": screen_size_inches,
        "resolution": resolution,
        "panel_technology": panel_technology,
        "refresh_rate_hz": refresh_rate_hz,
        "price": price,
        "sale_price": sale_price,
        "currency": currency,
        "is_available": is_available,
        "description": description,
        "specs_text": specs_text,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
