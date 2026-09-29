"""Typed contracts of the structured core.

Closed vocabularies are ``Enum``s; constructing one from an unknown string raises ``ValueError``.
Plan documents (the future Phase 4D LLM output and the Phase 4B gold plans) are parsed by
:func:`QueryPlanDelta.from_dict`, which rejects unknown keys at every level with
:class:`PlanValidationError` -- the same explicit style as ``evaluation/dataset.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class PlanValidationError(ValueError):
    """A plan document is structurally invalid (unknown key, bad enum, bad type)."""


class Intent(str, Enum):
    LOOKUP = "lookup"
    SPEC_QUESTION = "spec_question"
    COMPARE = "compare"
    LIST = "list"
    SUPERLATIVE = "superlative"
    RECOMMEND = "recommend"
    CLARIFY = "clarify"
    NON_RETRIEVAL = "non_retrieval"


class ContextMode(str, Enum):
    NEW = "new"
    REFINE = "refine"          # needs conversation state (Phase 4F)
    REFERENCE = "reference"    # needs conversation state (Phase 4F)


class Route(str, Enum):
    CLARIFY = "CLARIFY"
    NO_RETRIEVAL = "NO_RETRIEVAL"
    SQL_LOOKUP = "SQL_LOOKUP"
    SQL_FILTER = "SQL_FILTER"
    SQL_AGGREGATE = "SQL_AGGREGATE"
    CONSTRAINT_FIRST = "CONSTRAINT_FIRST"
    PRODUCT_SCOPED_SEMANTIC = "PRODUCT_SCOPED_SEMANTIC"
    SEMANTIC_FALLBACK = "SEMANTIC_FALLBACK"


SEMANTIC_ROUTES = frozenset({Route.PRODUCT_SCOPED_SEMANTIC, Route.SEMANTIC_FALLBACK})
STRUCTURED_ROUTES = frozenset({Route.SQL_LOOKUP, Route.SQL_FILTER, Route.SQL_AGGREGATE,
                               Route.CONSTRAINT_FIRST})


class SortKey(str, Enum):
    EFFECTIVE_PRICE = "effective_price"
    LIST_PRICE = "list_price"
    SCREEN_SIZE = "screen_size_inches"
    REFRESH_RATE = "refresh_rate_hz"


class SortDir(str, Enum):
    ASC = "asc"
    DESC = "desc"


class GroupKey(str, Enum):
    PANEL_TECHNOLOGY = "panel_technology"
    CATEGORY = "category"
    SCREEN_SIZE = "screen_size_inches"
    IS_AVAILABLE = "is_available"
    PRODUCT_KIND = "product_kind"


class ResolutionClass(str, Enum):
    UHD_4K = "4K"
    QHD = "QHD"
    FHD = "FHD"
    HD = "HD"


# Resolution class -> exact `products.resolution` values observed in the catalog (Phase 4B
# inventory). 4968x2808 (the professional Micro LED display) deliberately has no class.
RESOLUTION_VALUES = {
    ResolutionClass.UHD_4K: ("3840x2160",),
    ResolutionClass.QHD: ("2560x1440",),
    ResolutionClass.FHD: ("1920x1080",),
    ResolutionClass.HD: ("1366x768",),
}


class ProductKind(str, Enum):
    """Store product type, taken from the first word of the catalog product name (see
    ``catalog_repository.PRODUCT_KIND_SQL``): 73 'Телевизор', 2 'Дисплей'."""
    TV = "tv"
    DISPLAY = "display"
    OTHER = "other"


class FeatureState(str, Enum):
    YES = "yes"
    NO = "no"
    NOT_LISTED = "not_listed"


class Strength(str, Enum):
    REQUIRED = "required"
    PREFERRED = "preferred"


class RefKind(str, Enum):
    FULL = "full"
    FAMILY = "family"


# ---- plan input -------------------------------------------------------------------------

@dataclass(frozen=True)
class Range:
    min: Optional[float] = None
    max: Optional[float] = None

    @staticmethod
    def parse(raw: Any, where: str) -> "Range":
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return Range(float(raw), float(raw))
        if not isinstance(raw, dict):
            raise PlanValidationError(f"{where}: expected a number or {{min, max}}")
        _closed(raw, ("min", "max"), where)
        vals = {}
        for k in ("min", "max"):
            v = raw.get(k)
            if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0):
                raise PlanValidationError(f"{where}.{k}: must be a non-negative number")
            vals[k] = None if v is None else float(v)
        if vals["min"] is None and vals["max"] is None:
            raise PlanValidationError(f"{where}: empty range")
        if vals["min"] is not None and vals["max"] is not None and vals["min"] > vals["max"]:
            raise PlanValidationError(f"{where}: min > max")
        return Range(vals["min"], vals["max"])


@dataclass(frozen=True)
class Filters:
    """Hard constraints. Every field maps to one allowlisted SQL expression
    (``catalog_repository.compile_filters``); there is no free-form key."""
    panel_technology: tuple = ()
    category: tuple = ()
    resolution_class: tuple = ()          # ResolutionClass
    screen_size_inches: Optional[Range] = None
    effective_price: Optional[Range] = None
    list_price: Optional[Range] = None
    refresh_rate_hz: Optional[Range] = None
    is_available: Optional[bool] = None
    product_ids: tuple = ()               # internal products.id restriction (resolved refs only)
    exclude_product_kinds: tuple = ()     # ProductKind; set by the recommendation-scope policy

    USER_KEYS = ("panel_technology", "category", "resolution_class", "screen_size_inches",
                 "effective_price", "list_price", "refresh_rate_hz", "is_available")

    @staticmethod
    def from_dict(raw: Any, where: str = "constraints") -> "Filters":
        if raw is None:
            return Filters()
        if not isinstance(raw, dict):
            raise PlanValidationError(f"{where}: must be an object")
        _closed(raw, Filters.USER_KEYS, where)
        kw: dict = {}
        for key in ("panel_technology", "category"):
            if key in raw:
                kw[key] = _str_tuple(raw[key], f"{where}.{key}")
        if "resolution_class" in raw:
            kw["resolution_class"] = tuple(_enum(ResolutionClass, v, f"{where}.resolution_class")
                                           for v in _list(raw["resolution_class"], f"{where}.resolution_class"))
        for key in ("screen_size_inches", "effective_price", "list_price", "refresh_rate_hz"):
            if raw.get(key) is not None:
                kw[key] = Range.parse(raw[key], f"{where}.{key}")
        if raw.get("is_available") is not None:
            if not isinstance(raw["is_available"], bool):
                raise PlanValidationError(f"{where}.is_available: must be boolean")
            kw["is_available"] = raw["is_available"]
        return Filters(**kw)

    def user_constraint_keys(self) -> tuple:
        """Constraint fields the user actually set (policy fields excluded)."""
        out = []
        for key in self.USER_KEYS:
            v = getattr(self, key)
            if v not in (None, ()):
                out.append(key)
        return tuple(out)


@dataclass(frozen=True)
class Sort:
    key: SortKey
    direction: SortDir

    @staticmethod
    def from_dict(raw: Any, where: str = "sort") -> Optional["Sort"]:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise PlanValidationError(f"{where}: must be an object")
        _closed(raw, ("key", "dir"), where)
        return Sort(_enum(SortKey, raw.get("key"), f"{where}.key"),
                    _enum(SortDir, raw.get("dir"), f"{where}.dir"))


@dataclass(frozen=True)
class ModelRef:
    text: str
    kind: RefKind


@dataclass(frozen=True)
class FeatureRef:
    id: str
    strength: Strength


@dataclass(frozen=True)
class QueryPlanDelta:
    """Plan input contract (LLM output in Phase 4D; hand-authored gold plans in 4B).

    Feature / use-case / attribute ids are checked against the closed registry by
    ``planning.resolve_plan`` (this module does not import the registry)."""
    intent: Intent
    context_mode: ContextMode = ContextMode.NEW
    model_refs: tuple = ()                # ModelRef
    ordinal_refs: tuple = ()              # int (Phase 4F)
    constraints: Filters = field(default_factory=Filters)
    use_cases: tuple = ()                 # str (registry ids)
    features: tuple = ()                  # FeatureRef
    attributes_asked: tuple = ()          # str (registry feature ids)
    free_text_need: Optional[str] = None
    sort: Optional[Sort] = None
    limit: Optional[int] = None
    compare_size_inches: Optional[float] = None
    include_special_products: bool = False
    stated_year: Optional[int] = None
    language: str = "ru"
    clarification_reason: Optional[str] = None

    KEYS = ("intent", "context_mode", "model_refs", "ordinal_refs", "constraints", "use_cases",
            "features", "attributes_asked", "free_text_need", "sort", "limit",
            "compare_size_inches", "include_special_products", "stated_year", "language",
            "clarification_reason")

    @staticmethod
    def from_dict(raw: Any) -> "QueryPlanDelta":
        if not isinstance(raw, dict):
            raise PlanValidationError("plan: must be an object")
        _closed(raw, QueryPlanDelta.KEYS, "plan")
        refs = []
        for i, r in enumerate(_list(raw.get("model_refs", []), "model_refs")):
            if not isinstance(r, dict):
                raise PlanValidationError(f"model_refs[{i}]: must be an object")
            _closed(r, ("text", "kind"), f"model_refs[{i}]")
            text = r.get("text")
            if not isinstance(text, str) or not text.strip():
                raise PlanValidationError(f"model_refs[{i}].text: non-empty string required")
            refs.append(ModelRef(text.strip(), _enum(RefKind, r.get("kind", "full"), f"model_refs[{i}].kind")))
        feats = []
        for i, f in enumerate(_list(raw.get("features", []), "features")):
            if not isinstance(f, dict):
                raise PlanValidationError(f"features[{i}]: must be an object")
            _closed(f, ("id", "strength"), f"features[{i}]")
            fid = f.get("id")
            if not isinstance(fid, str) or not fid:
                raise PlanValidationError(f"features[{i}].id: string required")
            feats.append(FeatureRef(fid, _enum(Strength, f.get("strength", "preferred"), f"features[{i}].strength")))
        ordinals = _list(raw.get("ordinal_refs", []), "ordinal_refs")
        if any(not isinstance(o, int) or isinstance(o, bool) or o < 1 for o in ordinals):
            raise PlanValidationError("ordinal_refs: positive integers required")
        limit = raw.get("limit")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
            raise PlanValidationError("limit: positive integer required")
        size = raw.get("compare_size_inches")
        if size is not None and (not isinstance(size, (int, float)) or isinstance(size, bool) or size <= 0):
            raise PlanValidationError("compare_size_inches: positive number required")
        year = raw.get("stated_year")
        if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
            raise PlanValidationError("stated_year: integer required")
        need = raw.get("free_text_need")
        if need is not None and (not isinstance(need, str) or not need.strip()):
            raise PlanValidationError("free_text_need: non-empty string or null")
        special = raw.get("include_special_products", False)
        if not isinstance(special, bool):
            raise PlanValidationError("include_special_products: boolean required")
        return QueryPlanDelta(
            intent=_enum(Intent, raw.get("intent"), "intent"),
            context_mode=_enum(ContextMode, raw.get("context_mode", "new"), "context_mode"),
            model_refs=tuple(refs),
            ordinal_refs=tuple(ordinals),
            constraints=Filters.from_dict(raw.get("constraints")),
            use_cases=_str_tuple(raw.get("use_cases", []), "use_cases"),
            features=tuple(feats),
            attributes_asked=_str_tuple(raw.get("attributes_asked", []), "attributes_asked"),
            free_text_need=need.strip() if need else None,
            sort=Sort.from_dict(raw.get("sort")),
            limit=limit,
            compare_size_inches=None if size is None else float(size),
            include_special_products=special,
            stated_year=year,
            language=str(raw.get("language", "ru")),
            clarification_reason=raw.get("clarification_reason"),
        )


# ---- catalog rows -----------------------------------------------------------------------

@dataclass(frozen=True)
class ProductRow:
    id: int                   # internal products.id
    source: str
    external_id: str
    model_code: Optional[str]
    name: str
    category: Optional[str]
    series: Optional[str]
    product_kind: ProductKind
    year: Optional[int]
    screen_size_inches: Optional[float]
    resolution: Optional[str]
    panel_technology: Optional[str]
    refresh_rate_hz: Optional[int]
    price: Optional[float]
    sale_price: Optional[float]
    effective_price: Optional[float]  # computed by the canonical SQL expression only
    currency: str
    is_available: bool
    product_url: Optional[str]

    @property
    def identity(self) -> tuple:
        """Canonical identity ``(source, external_id)``."""
        return (self.source, self.external_id)


@dataclass(frozen=True)
class SpecRow:
    product_id: int
    spec_group: Optional[str]
    spec_name: str
    spec_key: str
    spec_value: Optional[str]


# ---- feature evaluation / ranking -------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    origin: str               # "column" | "spec"
    name: str                 # column name or spec_name
    value: Optional[str]      # raw value, never rewritten


@dataclass(frozen=True)
class FeatureResult:
    feature_id: str
    state: FeatureState
    value: Optional[float] = None         # numeric features only
    evidence: tuple = ()                  # Evidence
    data_quality: Optional[str] = None    # e.g. "malformed_component", "unit_scale_mismatch"


@dataclass(frozen=True)
class NumericSignal:
    """Deterministic tie-break inside equal preferred-feature counts.
    ``source`` is a numeric feature id or the ``screen_size_inches`` column."""
    source: str
    direction: SortDir


@dataclass(frozen=True)
class RankedCandidate:
    product: ProductRow
    rank: int                              # 1-based position in its bucket
    required: dict                         # feature_id -> FeatureState
    preferred_matched: int
    preferred_total: int
    numeric_signals: tuple                 # (source, value | None) in signal order
    features: dict                         # feature_id -> FeatureResult

    def explain(self) -> dict:
        return {"model_code": self.product.model_code, "rank": self.rank,
                "preferred_matched": self.preferred_matched, "preferred_total": self.preferred_total,
                "numeric_signals": [list(s) for s in self.numeric_signals],
                "effective_price": self.product.effective_price,
                "required": {k: v.value for k, v in self.required.items()}}


@dataclass(frozen=True)
class RankingResult:
    policy: str
    qualified: tuple          # RankedCandidate: all required features == yes
    unknown: tuple            # RankedCandidate: some required feature not_listed, none == no
    rejected: tuple           # ProductRow: some required feature == no

    def ranked_codes(self) -> list:
        return [c.product.model_code for c in self.qualified] + [c.product.model_code for c in self.unknown]


@dataclass(frozen=True)
class Shortlist:
    policy: str
    items: tuple              # RankedCandidate, in ranking order (composition never reorders)
    tiers: dict = field(default_factory=dict)   # model_code -> "low" | "mid" | "high" (tier policy)
    notes: tuple = ()


# ---- resolution ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelResolution:
    ref: ModelRef
    status: str                # "exact" | "family" | "not_found"
    products: tuple = ()       # ProductRow
    basis: Optional[str] = None           # "model_code" | "series" | "model_code_stem"
    sizes: tuple = ()          # distinct screen sizes of the matched products
    suggestions: tuple = ()    # nearest catalog model codes for not_found full refs


@dataclass(frozen=True)
class FamilyComparison:
    families: tuple            # family tokens
    status: str                # resolved_user_size | resolved_single_shared_size |
                               # ambiguous_multiple_shared_sizes | size_not_shared | no_shared_size
    shared_sizes: tuple
    selected_size: Optional[float]
    products: tuple = ()       # ProductRow at selected_size (one per family, when resolved)
    common_facts: dict = field(default_factory=dict)   # family -> {typed column: value if uniform}


@dataclass(frozen=True)
class Gap:
    kind: str
    detail: str
    model_codes: tuple = ()


# ---- resolved plan / route ----------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedPlan:
    """Validated plan with deterministic policy defaults applied (``planning.resolve_plan``)."""
    intent: Intent
    context_mode: ContextMode
    filters: Filters                       # user constraints + policy (availability, scope)
    user_constraint_keys: tuple            # which filter fields the user set
    sort: Optional[Sort]
    limit: Optional[int]
    resolutions: tuple                     # ModelResolution, one per model ref
    family_comparison: Optional[FamilyComparison]
    use_cases: tuple
    required: tuple                        # feature ids
    preferred: tuple                       # feature ids (explicit + use-case profiles, deduped)
    numeric: tuple                         # NumericSignal
    attributes_asked: tuple                # feature ids
    free_text_need: Optional[str]
    stated_year: Optional[int]
    ordinal_refs: tuple
    clarification_reason: Optional[str]
    gaps: tuple                            # Gap known at planning time (not_found refs, profile gaps)
    policy_notes: tuple                    # e.g. "availability_default=available_only"
    availability_default_applied: bool = False
    scope_excluded_kinds: tuple = ()

    @property
    def has_budget(self) -> bool:
        return self.filters.effective_price is not None or self.filters.list_price is not None

    @property
    def resolved_products(self) -> tuple:
        seen, out = set(), []
        for r in self.resolutions:
            for p in r.products:
                if p.id not in seen:
                    seen.add(p.id)
                    out.append(p)
        return tuple(out)


@dataclass(frozen=True)
class RoutePlan:
    route: Route
    rule: str                  # id of the router rule that fired (for logs/tests)
    executable_in_4b: bool


# ---- helpers --------------------------------------------------------------------------------

def _closed(raw: dict, allowed: tuple, where: str) -> None:
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise PlanValidationError(f"{where}: unknown key(s) {unknown}")


def _enum(enum_cls, value: Any, where: str):
    try:
        return enum_cls(value)
    except ValueError:
        raise PlanValidationError(
            f"{where}: {value!r} not in {[e.value for e in enum_cls]}") from None


def _list(raw: Any, where: str) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PlanValidationError(f"{where}: must be a list")
    return raw


def _str_tuple(raw: Any, where: str) -> tuple:
    items = _list(raw, where)
    if any(not isinstance(x, str) or not x.strip() for x in items):
        raise PlanValidationError(f"{where}: non-empty strings required")
    return tuple(x.strip() for x in items)
