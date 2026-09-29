"""Feature registry v1: closed, versioned needs -> deterministic predicates over catalog data.

Every predicate was written against the values observed by the Phase 4B read-only inventory
(``evaluation/results/consultant_inventory_4b.json``), not against the Phase 4A proposal.

Tri-state rule (uniform across features):

* ``yes``        -- the catalog states the feature (typed column or spec value).
* ``no``         -- the catalog states an explicit negative: a typed/numeric comparison, the
                    spec value ``Нет``, or an explicit value of a different kind (e.g. FreeSync
                    tier ``FreeSync Premium`` for ``freesync_premium_pro``, ``ARC`` for ``earc``).
* ``not_listed`` -- the spec is absent, or present but does not mention the feature. A
                    multi-value list that omits an item is *not* read as a negative.

Numeric features yield a parsed value, or ``not_listed`` with a ``data_quality`` reason when
the raw value exists but cannot be parsed safely. Values are never guessed or repaired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .schemas import Evidence, FeatureResult, FeatureState, NumericSignal, ProductRow, SortDir, SpecRow

REGISTRY_VERSION = "features-v1"

YES, NO, NOT_LISTED = FeatureState.YES, FeatureState.NO, FeatureState.NOT_LISTED

SPEC_OTHER_PICTURE = "Другие технологии оптимизации изображения"
SPEC_FREESYNC = "Технология FreeSync"
SPEC_ALLM = "Автовключение игрового режима (ALLM)"
SPEC_GAME_BAR = "Игровая панель (Game Bar)"
SPEC_HDMI_VERSION = "Версия HDMI"
SPEC_HDMI_ARC = "Технология HDMI ARC"
SPEC_ANTI_GLARE = "Антибликовое покрытие"
SPEC_VIEWING_MODES = "Режимы просмотра"
SPEC_SOUND_FORMATS = "Поддержка форматов звука"
SPEC_SOUND_POWER = "Мощность звука, Вт"
SPEC_DIMENSIONS = "Размер без подставки (ШxВxГ), см"
SPEC_VESA = "Стандарт VESA"

_NEGATIVE = {"нет", "no"}
_YES_WORDS = {"да", "yes"}


@dataclass(frozen=True)
class FeatureDef:
    id: str
    kind: str                  # "bool" | "numeric"
    section: str               # chunk section holding the evidence (for Phase 4C passages)
    spec_names: tuple          # spec rows the evaluator reads (empty for typed-column features)
    evaluate: Callable[[ProductRow, dict], FeatureResult]
    note: str = ""


# ---- evaluators -------------------------------------------------------------------------

def _spec(specs: dict, name: str) -> Optional[str]:
    v = specs.get(name)
    return None if v is None or not str(v).strip() else str(v)


def _ev(name: str, value: Optional[str]) -> tuple:
    return (Evidence("spec", name, value),)


def _contains(fid: str, name: str, needles: Sequence[str]):
    """yes if the spec value mentions any needle; explicit 'Нет' -> no; otherwise not_listed."""
    def run(p: ProductRow, specs: dict) -> FeatureResult:
        v = _spec(specs, name)
        if v is None:
            return FeatureResult(fid, NOT_LISTED)
        low = v.lower()
        if any(n.lower() in low for n in needles):
            return FeatureResult(fid, YES, evidence=_ev(name, v))
        if low.strip() in _NEGATIVE:
            return FeatureResult(fid, NO, evidence=_ev(name, v))
        return FeatureResult(fid, NOT_LISTED, evidence=_ev(name, v))
    return run


def _flag(fid: str, name: str, yes_values: Sequence[str] = ()):
    """Presence spec whose catalog values are 'Да' (plus listed synonyms); 'Нет' -> no."""
    accepted = {x.lower() for x in (*_YES_WORDS, *yes_values)}
    def run(p: ProductRow, specs: dict) -> FeatureResult:
        v = _spec(specs, name)
        if v is None:
            return FeatureResult(fid, NOT_LISTED)
        low = v.strip().lower()
        if low in accepted:
            return FeatureResult(fid, YES, evidence=_ev(name, v))
        if low in _NEGATIVE:
            return FeatureResult(fid, NO, evidence=_ev(name, v))
        return FeatureResult(fid, NOT_LISTED, evidence=_ev(name, v), data_quality="unrecognized_value")
    return run


def _tiered(fid: str, name: str, needle: str):
    """A spec whose value names a tier: needle present -> yes; another explicit tier -> no."""
    def run(p: ProductRow, specs: dict) -> FeatureResult:
        v = _spec(specs, name)
        if v is None:
            return FeatureResult(fid, NOT_LISTED)
        return FeatureResult(fid, YES if needle.lower() in v.lower() else NO, evidence=_ev(name, v))
    return run


def _hz_120(p: ProductRow, specs: dict) -> FeatureResult:
    if p.refresh_rate_hz is None:
        return FeatureResult("hz_120", NOT_LISTED)
    ev = (Evidence("column", "refresh_rate_hz", str(p.refresh_rate_hz)),)
    return FeatureResult("hz_120", YES if p.refresh_rate_hz >= 120 else NO,
                         value=float(p.refresh_rate_hz), evidence=ev)


def _hdmi_2_1(p: ProductRow, specs: dict) -> FeatureResult:
    v = _spec(specs, SPEC_HDMI_VERSION)
    if v is None:
        return FeatureResult("hdmi_2_1", NOT_LISTED)
    m = re.search(r"(\d+(?:\.\d+)?)", v)
    if not m:
        return FeatureResult("hdmi_2_1", NOT_LISTED, evidence=_ev(SPEC_HDMI_VERSION, v),
                             data_quality="unrecognized_value")
    return FeatureResult("hdmi_2_1", YES if float(m.group(1)) >= 2.1 else NO, evidence=_ev(SPEC_HDMI_VERSION, v))


def _earc(p: ProductRow, specs: dict) -> FeatureResult:
    v = _spec(specs, SPEC_HDMI_ARC)
    if v is None:
        return FeatureResult("earc", NOT_LISTED)
    low = v.lower()
    if "earc" in low:
        return FeatureResult("earc", YES, evidence=_ev(SPEC_HDMI_ARC, v))
    if low.strip() in ("arc", *_NEGATIVE):
        return FeatureResult("earc", NO, evidence=_ev(SPEC_HDMI_ARC, v))
    return FeatureResult("earc", NOT_LISTED, evidence=_ev(SPEC_HDMI_ARC, v), data_quality="unrecognized_value")


_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")


def _sound_power(p: ProductRow, specs: dict) -> FeatureResult:
    v = _spec(specs, SPEC_SOUND_POWER)
    if v is None:
        return FeatureResult("sound_power_w", NOT_LISTED)
    s = v.strip().replace(",", ".")
    if not _NUMBER.match(s):
        return FeatureResult("sound_power_w", NOT_LISTED, evidence=_ev(SPEC_SOUND_POWER, v),
                             data_quality="unparseable_number")
    return FeatureResult("sound_power_w", YES, value=float(s), evidence=_ev(SPEC_SOUND_POWER, v))


# Width of a 16:9 panel in cm is ~2.21 x diagonal in inches; observed catalog widths without
# stand are 1.00-1.12 x that. Outside [0.8, 1.3] the string is on another scale (e.g. mm).
WIDTH_PER_INCH_CM = 2.214
WIDTH_RATIO_BOUNDS = (0.8, 1.3)
MAX_PLAUSIBLE_DEPTH_CM = 15.0     # deepest observed panel without stand: 7.72 cm


def parse_dimensions_cm(raw: Optional[str], screen_size_inches: Optional[float]):
    """Parse 'W x H x D' (cm). Returns ``((w, h, d), None)`` or ``(None, data_quality_reason)``.

    Tolerated (unambiguous): Cyrillic 'х' as separator, tabs/extra whitespace, decimal comma.
    Rejected (reason, never repaired): a component that is not one number (e.g. '70.8.8'),
    not exactly three components, a width implausible for the diagonal (unit/scale mismatch),
    a depth outside (0, 15] cm.
    """
    if raw is None or not str(raw).strip():
        return None, None
    s = str(raw).replace("х", "x").replace("Х", "x").replace("X", "x")
    parts = [c.strip().replace(",", ".") for c in re.split(r"\s*x\s*", s.strip())]
    if len(parts) != 3:
        return None, "unexpected_component_count"
    if not all(_NUMBER.match(c) for c in parts):
        return None, "malformed_component"
    w, h, d = (float(c) for c in parts)
    if screen_size_inches:
        ratio = w / (float(screen_size_inches) * WIDTH_PER_INCH_CM)
        if not WIDTH_RATIO_BOUNDS[0] <= ratio <= WIDTH_RATIO_BOUNDS[1]:
            return None, "unit_scale_mismatch"
    if not 0 < d <= MAX_PLAUSIBLE_DEPTH_CM:
        return None, "implausible_depth"
    return (w, h, d), None


def _depth(p: ProductRow, specs: dict) -> FeatureResult:
    v = _spec(specs, SPEC_DIMENSIONS)
    if v is None:
        return FeatureResult("depth_cm", NOT_LISTED)
    dims, reason = parse_dimensions_cm(v, p.screen_size_inches)
    if dims is None:
        return FeatureResult("depth_cm", NOT_LISTED, evidence=_ev(SPEC_DIMENSIONS, v), data_quality=reason)
    return FeatureResult("depth_cm", YES, value=dims[2], evidence=_ev(SPEC_DIMENSIONS, v))


def _vesa(p: ProductRow, specs: dict) -> FeatureResult:
    v = _spec(specs, SPEC_VESA)
    if v is None:
        return FeatureResult("vesa", NOT_LISTED)
    if re.search(r"\d+\s*[xх]\s*\d+", v):
        return FeatureResult("vesa", YES, evidence=_ev(SPEC_VESA, v))
    if v.strip().lower() in _NEGATIVE:
        return FeatureResult("vesa", NO, evidence=_ev(SPEC_VESA, v))
    return FeatureResult("vesa", NOT_LISTED, evidence=_ev(SPEC_VESA, v), data_quality="unrecognized_value")


# ---- registry -----------------------------------------------------------------------------

_DEFS = (
    FeatureDef("hz_120", "bool", "display", (), _hz_120, "typed column refresh_rate_hz >= 120"),
    FeatureDef("vrr", "bool", "display", (SPEC_OTHER_PICTURE,),
               _contains("vrr", SPEC_OTHER_PICTURE, ("Variable Refresh Rate",)),
               "34/75 list it; absent from the list -> not_listed"),
    FeatureDef("freesync_premium", "bool", "gaming", (SPEC_FREESYNC,),
               _contains("freesync_premium", SPEC_FREESYNC, ("FreeSync Premium",)),
               "values: 'FreeSync Premium' 15, 'FreeSync Premium Pro' 24"),
    FeatureDef("freesync_premium_pro", "bool", "gaming", (SPEC_FREESYNC,),
               _tiered("freesync_premium_pro", SPEC_FREESYNC, "Premium Pro"),
               "'FreeSync Premium' (explicit lower tier) -> no"),
    FeatureDef("allm", "bool", "gaming", (SPEC_ALLM,), _flag("allm", SPEC_ALLM), "only 'Да' observed, 16/75"),
    FeatureDef("game_bar", "bool", "gaming", (SPEC_GAME_BAR,), _flag("game_bar", SPEC_GAME_BAR),
               "only 'Да' observed, 50/75"),
    FeatureDef("hdmi_2_1", "bool", "connectivity", (SPEC_HDMI_VERSION,), _hdmi_2_1,
               "'Версия HDMI' exists on 1/75 products; mostly not_listed"),
    FeatureDef("earc", "bool", "connectivity", (SPEC_HDMI_ARC,), _earc, "'eARC' 73, 'ARC' 1"),
    FeatureDef("anti_glare", "bool", "display", (SPEC_ANTI_GLARE,),
               _flag("anti_glare", SPEC_ANTI_GLARE, ("Anti Reflection",)), "'Да' 24, 'Anti Reflection' 3"),
    FeatureDef("filmmaker_mode", "bool", "display", (SPEC_VIEWING_MODES,),
               _contains("filmmaker_mode", SPEC_VIEWING_MODES, ("Filmmaker", "режиссер", "режиссёр")),
               "69/75 -- near-universal, weak discriminator"),
    FeatureDef("dolby_atmos", "bool", "audio", (SPEC_SOUND_FORMATS,),
               _contains("dolby_atmos", SPEC_SOUND_FORMATS, ("Atmos",)),
               "62/75; 3 list only 'Dolby Digital' -> not_listed"),
    FeatureDef("sound_power_w", "numeric", "audio", (SPEC_SOUND_POWER,), _sound_power, "75/75, all numeric"),
    FeatureDef("depth_cm", "numeric", "physical_design", (SPEC_DIMENSIONS,), _depth,
               "59/75; see parse_dimensions_cm for data-quality rules"),
    FeatureDef("vesa", "bool", "physical_design", (SPEC_VESA,), _vesa, "62/75; one explicit 'Нет'"),
)

FEATURES = {d.id: d for d in _DEFS}
REGISTRY_SPEC_NAMES = tuple(sorted({n for d in _DEFS for n in d.spec_names}))


@dataclass(frozen=True)
class UseCaseProfile:
    id: str
    preferred: tuple                  # feature ids
    numeric: tuple = ()               # NumericSignal
    gaps: tuple = ()                  # (kind, detail) data gaps inherent to the need


USE_CASES = {
    # hdmi_2_1 is deliberately NOT a gaming signal: it is listed for 1/75 products (the
    # professional display), so it would rank listing completeness, not capability.
    "gaming": UseCaseProfile("gaming", ("hz_120", "vrr", "freesync_premium", "allm", "game_bar")),
    "movies": UseCaseProfile("movies", ("filmmaker_mode", "dolby_atmos")),
    "sound": UseCaseProfile("sound", ("dolby_atmos",), (NumericSignal("sound_power_w", SortDir.DESC),)),
    "bright_room": UseCaseProfile("bright_room", ("anti_glare",), (), (
        ("brightness_not_in_catalog",
         "The catalog has no brightness (nits) specification; anti-glare coating is not a brightness measure."),)),
    "thin_wall": UseCaseProfile("thin_wall", ("vesa",), (NumericSignal("depth_cm", SortDir.ASC),)),
    "compact": UseCaseProfile("compact", (), (NumericSignal("screen_size_inches", SortDir.ASC),)),
}
PROFILES_VERSION = "use-cases-v1"

NUMERIC_COLUMN_SIGNALS = ("screen_size_inches",)


def evaluate_features(products: Sequence[ProductRow], specs_by_product: dict,
                      feature_ids: Sequence[str]) -> dict:
    """``{product_id: {feature_id: FeatureResult}}``. Unknown feature ids raise ``KeyError``."""
    defs = [FEATURES[f] for f in feature_ids]
    out = {}
    for p in products:
        specs = {s.spec_name: s.spec_value for s in specs_by_product.get(p.id, ())}
        out[p.id] = {d.id: d.evaluate(p, specs) for d in defs}
    return out


def spec_names_for(feature_ids: Sequence[str]) -> tuple:
    return tuple(sorted({n for f in feature_ids for n in FEATURES[f].spec_names}))


def numeric_value(signal: NumericSignal, product: ProductRow, results: dict) -> Optional[float]:
    if signal.source in NUMERIC_COLUMN_SIGNALS:
        v = getattr(product, signal.source)
        return None if v is None else float(v)
    r: Optional[FeatureResult] = results.get(signal.source)
    return None if r is None or r.state is not YES else r.value
