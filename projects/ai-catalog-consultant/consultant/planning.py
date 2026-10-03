"""Plan validation and deterministic policy defaults: ``QueryPlanDelta -> ResolvedPlan``.

Policies (all explicit, all tested, each recorded in ``ResolvedPlan.policy_notes``):

* **Closed vocabularies.** Unknown use-case / feature / attribute ids, panel technologies and
  categories raise :class:`PlanValidationError`.
* **Availability default** (``AVAILABILITY_DEFAULT_INTENTS``): recommend / list / superlative
  consider available products only unless the plan sets ``is_available``; lookup, spec
  question and compare see every product.
* **Recommendation scope** (``SCOPE_POLICY``): ``recommend`` excludes store product kind
  ``display`` (product name starts with 'Дисплей': the professional Micro LED display and the
  portable Movingstyle) unless the plan names a category, names models, or sets
  ``include_special_products``. Nothing is excluded for lookup / spec / compare / list /
  superlative -- factual catalog-wide questions see the whole catalog.
* **Family comparison**: never silently picks a size (see :func:`compare_families`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Sequence

from .features import FEATURES, USE_CASES
from .schemas import (
    FamilyComparison, Filters, Gap, Intent, ModelResolution, PlanValidationError, ProductKind,
    QueryPlanDelta, RefKind, ResolvedPlan, Strength,
)
from .vocabulary import CatalogVocabulary

AVAILABILITY_DEFAULT_INTENTS = frozenset({Intent.RECOMMEND, Intent.LIST, Intent.SUPERLATIVE})
SCOPE_POLICY = "recommendation-scope-v1"
SCOPE_EXCLUDED_KINDS = (ProductKind.DISPLAY,)
FAMILY_COMMON_COLUMNS = ("panel_technology", "refresh_rate_hz", "resolution", "category")


def compare_families(resolutions: Sequence[ModelResolution], user_size: Optional[float]) -> FamilyComparison:
    """Size handling for family comparisons ("S95H vs S90H").

    * user size given and offered by every family -> ``resolved_user_size``;
    * user size given but missing in some family  -> ``size_not_shared``;
    * exactly one shared size                     -> ``resolved_single_shared_size``;
    * several shared sizes                        -> ``ambiguous_multiple_shared_sizes`` (options
      = shared sizes; the caller must clarify -- never the largest by default);
    * none shared                                 -> ``no_shared_size``.
    """
    fams = [r for r in resolutions if r.status == "family"]
    tokens = tuple(r.ref.text for r in fams)
    shared = set(fams[0].sizes) if fams else set()
    for r in fams[1:]:
        shared &= set(r.sizes)
    shared_sizes = tuple(sorted(shared))
    common = {}
    for r in fams:
        facts = {}
        for col in FAMILY_COMMON_COLUMNS:
            values = {getattr(p, col) for p in r.products}
            facts[col] = values.pop() if len(values) == 1 else None
        common[r.ref.text] = facts
    if user_size is not None:
        status = "resolved_user_size" if user_size in shared else "size_not_shared"
        selected = user_size if user_size in shared else None
    elif len(shared_sizes) == 1:
        status, selected = "resolved_single_shared_size", shared_sizes[0]
    elif shared_sizes:
        status, selected = "ambiguous_multiple_shared_sizes", None
    else:
        status, selected = "no_shared_size", None
    products = ()
    if selected is not None:
        products = tuple(p for r in fams for p in r.products if p.screen_size_inches == selected)
    return FamilyComparison(tokens, status, shared_sizes, selected, products, common)


def _canonical_values(values: tuple, kind: str, vocab: CatalogVocabulary) -> tuple:
    out = []
    for v in values:
        c = vocab.canonical(kind, v)
        if c is None:
            raise PlanValidationError(f"constraints.{kind}: {v!r} is not a catalog value")
        out.append(c)
    return tuple(out)


def _dedupe(items) -> tuple:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return tuple(out)


def resolve_plan(delta: QueryPlanDelta, repo, vocab: Optional[CatalogVocabulary] = None) -> ResolvedPlan:
    """``repo`` provides ``vocabulary()`` and ``resolve_model_refs()`` (a ``CatalogRepository``)."""
    vocab = vocab or repo.vocabulary()
    for uc in delta.use_cases:
        if uc not in USE_CASES:
            raise PlanValidationError(f"use_cases: unknown id {uc!r}")
    for f in delta.features:
        if f.id not in FEATURES:
            raise PlanValidationError(f"features: unknown id {f.id!r}")
    for a in delta.attributes_asked:
        if a not in FEATURES:
            raise PlanValidationError(f"attributes_asked: unknown id {a!r}")

    c = delta.constraints
    filters = replace(c, panel_technology=_canonical_values(c.panel_technology, "panel_technology", vocab),
                      category=_canonical_values(c.category, "category", vocab))
    user_keys = filters.user_constraint_keys()
    notes, gaps = [], []

    availability_default = delta.intent in AVAILABILITY_DEFAULT_INTENTS and filters.is_available is None
    if availability_default:
        filters = replace(filters, is_available=True)
        notes.append("availability_default=available_only")

    scope_kinds: tuple = ()
    if (delta.intent is Intent.RECOMMEND and not delta.include_special_products
            and not filters.category and not delta.model_refs):
        scope_kinds = SCOPE_EXCLUDED_KINDS
        filters = replace(filters, exclude_product_kinds=tuple(k.value for k in scope_kinds))
        notes.append(f"{SCOPE_POLICY}=exclude:{','.join(k.value for k in scope_kinds)}")

    resolutions = tuple(repo.resolve_model_refs(delta.model_refs, vocab)) if delta.model_refs else ()
    for r in resolutions:
        if r.status == "not_found":
            gaps.append(Gap("model_not_found", f"{r.ref.text!r} is not in the catalog", r.suggestions))

    family_cmp = None
    fam_refs = [r for r in resolutions if r.ref.kind is RefKind.FAMILY and r.status == "family"]
    if delta.intent is Intent.COMPARE and len(fam_refs) >= 2:
        family_cmp = compare_families(fam_refs, delta.compare_size_inches)

    required = _dedupe(f.id for f in delta.features if f.strength is Strength.REQUIRED)
    explicit_pref = [f.id for f in delta.features if f.strength is Strength.PREFERRED]
    profile_pref = [fid for uc in delta.use_cases for fid in USE_CASES[uc].preferred]
    preferred = tuple(f for f in _dedupe([*explicit_pref, *profile_pref]) if f not in required)
    numeric = _dedupe(s for uc in delta.use_cases for s in USE_CASES[uc].numeric)
    for uc in delta.use_cases:
        for kind, detail in USE_CASES[uc].gaps:
            gaps.append(Gap(kind, detail))

    if delta.sort is not None and (required or preferred or numeric):
        gaps.append(Gap("preferences_not_applied_to_ordering",
                        "An explicit ordering (sort) takes precedence; feature preferences are not ranked."))
    if delta.stated_year is not None and delta.stated_year not in vocab.years:
        gaps.append(Gap("year_not_in_catalog",
                        f"User stated year {delta.stated_year}; catalog years are {sorted(vocab.years)}. "
                        "The structured year column is authoritative and is not filtered on."))

    return ResolvedPlan(
        intent=delta.intent, context_mode=delta.context_mode, filters=filters,
        user_constraint_keys=user_keys, sort=delta.sort, limit=delta.limit, resolutions=resolutions,
        family_comparison=family_cmp, use_cases=delta.use_cases, required=required,
        preferred=preferred, numeric=numeric, attributes_asked=delta.attributes_asked,
        free_text_need=delta.free_text_need, stated_year=delta.stated_year,
        ordinal_refs=delta.ordinal_refs, clarification_reason=delta.clarification_reason,
        gaps=tuple(gaps), policy_notes=tuple(notes),
        availability_default_applied=availability_default, scope_excluded_kinds=scope_kinds,
    )
