"""Phase 4C evidence layer: ``ResolvedPlan`` + 4B ``StructuredResult`` -> ``EvidenceBundle``.

The bundle is the authoritative input for future answer generation (Phase 4E). No LLM is called.

Rules enforced here (tested):

* Prices and availability come only from live ``products`` rows (``ProductRow``). Overview chunk
  lines ``Цена:`` / ``Наличие:`` are index-time snapshots and are stripped from every passage.
* Fact ids are deterministic and unambiguous: ``P1.col.<column>``, ``P1.spec.<spec_key>`` (unique
  per product by the ``UNIQUE(product_id, spec_key)`` constraint), ``P1.feat.<feature_id>``.
* Mapped features get passages by deterministic section lookup (0 embeddings). Vector search is
  used only for a ``free_text_need`` (candidate-scoped, product-scoped) or the global fallback,
  and only with a cached vector -- a missing vector is an explicit ``semantic_unavailable`` gap.
* Vector search never widens the structured candidate set (checked; violations raise).
* Semantic similarity may reorder products only inside identical structured fit
  (``semantic.semantic_tiebreak``), only when explicitly enabled (default off; measured harmful
  in Phase 4C); structured / final ranks are recorded.
* A vector miss is never evidence of absence; absence comes from ``semantic.lexical_probe``.
* Confidence is structural (route, gaps, discrimination), never a cosine threshold.
* Size is bounded (products, passages per product, token budget); gaps are never trimmed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence

from indexing.metadata import approx_token_count

from .catalog_repository import CatalogRepository
from .features import FEATURES, NUMERIC_COLUMN_SIGNALS, spec_names_for
from .ranking import MAX_SHORTLIST, _top_band, choose_policy, compose_shortlist
from .relaxation import alternative_rows, relax
from .retrieval import StructuredResult
from .schemas import RESOLUTION_VALUES, Intent, ProductRow, Range, ResolvedPlan, Route, RoutePlan
from .semantic import (
    EmbeddingUnavailable, best_similarity_by_product, catalog_coverage, lexical_probe, probe_terms,
    semantic_tiebreak,
)

EVIDENCE_VERSION = "evidence-v1"
MAX_PRODUCTS = MAX_SHORTLIST
MAX_LIST_PRODUCTS = 20
MAX_NAMED_PRODUCTS = 4
MAX_PASSAGES_PER_PRODUCT = 2
PRODUCT_SCOPED_PASSAGES = 3
FALLBACK_PRODUCTS = 15            # vector-first window before structural validation (Phase 4A §11.2)
TOKEN_BUDGET = 6000
NON_OVERVIEW_SECTIONS = ("display", "gaming", "audio", "smart_features", "connectivity", "physical_design")

GAP_KINDS = (
    "model_not_found", "attribute_not_in_catalog", "attribute_not_listed_for_product",
    "attribute_absence_unverified", "no_product_satisfies", "required_feature_not_listed", "data_quality",
    "excluded_unavailable", "excluded_out_of_scope", "not_in_catalog_domain", "semantic_unavailable",
    "year_mismatch", "family_size_ambiguous", "preferences_not_applied_to_ordering", "candidates_truncated",
    "top_n_ties_not_expanded",
)
_FROM_4B = {
    "model_not_found": "model_not_found",
    "attribute_not_listed_for_product": "attribute_not_listed_for_product",
    "no_product_satisfies": "no_product_satisfies",
    "required_feature_not_confirmed": "required_feature_not_listed",
    "brightness_not_in_catalog": "not_in_catalog_domain",
    "year_not_in_catalog": "year_mismatch",
    "stated_year_in_name_only": "year_mismatch",
    "preferences_not_applied_to_ordering": "preferences_not_applied_to_ordering",
    "candidates_truncated": "candidates_truncated",
    "top_n_ties_not_expanded": "top_n_ties_not_expanded",
}
_STALE_LINE = re.compile(r"^\s*(Цена|Наличие)\s*:")
_SAFE_KEY = re.compile(r"^[a-z0-9_]+$")
_COLUMNS = (("category", "Категория", None), ("series", "Серия", None), ("panel_technology", "Тип экрана", None),
            ("screen_size_inches", "Диагональ", "inch"), ("resolution", "Разрешение", None),
            ("refresh_rate_hz", "Частота обновления", "Hz"), ("year", "Год", None),
            ("effective_price", "Цена (действующая)", "RUB"), ("price", "Цена без скидки", "RUB"),
            ("sale_price", "Цена со скидкой", "RUB"), ("is_available", "Наличие", None))


# ---- contract -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class FactItem:
    fact_id: str
    label: str
    value: str
    unit: Optional[str]
    origin: str                      # "products" | "product_specs"


@dataclass(frozen=True)
class Passage:
    chunk_id: int
    product_id: int
    source: str
    external_id: str
    section: str
    text: str                        # sanitized
    retrieval: str                   # "section_lookup" | "vector"
    similarity: Optional[float] = None   # diagnostic only; never rendered for the LLM
    stripped_lines: int = 0


@dataclass(frozen=True)
class FeatureStatus:
    feature_id: str
    state: str                       # yes | no | not_listed
    value: Optional[float]
    fact_ids: tuple
    data_quality: Optional[str] = None


@dataclass(frozen=True)
class ConstraintStatus:
    key: str
    requested: str
    actual: Optional[str]
    satisfied: Optional[bool]        # None when the product has no value for it


@dataclass(frozen=True)
class ProductEvidence:
    handle: str
    product_id: int
    source: str
    external_id: str
    model_code: Optional[str]
    name: str
    url: Optional[str]
    facts: tuple                     # FactItem
    features: tuple                  # FeatureStatus
    constraints: tuple               # ConstraintStatus
    passages: tuple                  # Passage
    selection_reasons: tuple         # LLM-safe strings
    ranking_debug: dict = field(default_factory=dict)   # structured/semantic/final rank, fit; not for the LLM


@dataclass(frozen=True)
class Gap:
    kind: str
    detail: str
    code: Optional[str] = None
    handles: tuple = ()
    model_codes: tuple = ()


@dataclass(frozen=True)
class EvidenceBundle:
    version: str
    intent: str
    route: str
    router_rule: str
    plan_summary: dict
    products: tuple                  # ProductEvidence, P1..Pn in shortlist/result order
    alternatives: tuple              # ProductEvidence, A1..An, unique: relaxation rows labelled with the
                                     # constraints they actually violate, then required-not-listed candidates
    gaps: tuple
    totals: dict
    retrieval_confidence: str        # strong | partial | weak | not_applicable
    confidence_reasons: tuple
    semantic: dict                   # path, embedding source, candidate count, escapes, tie-break report
    budget: dict                     # approx tokens, limit, trimmed passages


# ---- pure helpers -------------------------------------------------------------------------------

def sanitize_passage(text: str) -> tuple:
    """Drop index-time ``Цена:`` / ``Наличие:`` lines. Returns ``(text, removed_line_count)``."""
    kept, removed = [], 0
    for line in text.splitlines():
        if _STALE_LINE.match(line):
            removed += 1
        else:
            kept.append(line)
    return "\n".join(kept).strip(), removed


def fact_id(handle: str, kind: str, key: str) -> str:
    if kind not in ("col", "spec", "feat") or not _SAFE_KEY.match(key):
        raise ValueError(f"unsafe fact id component {kind!r}/{key!r}")
    return f"{handle}.{kind}.{key}"


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def column_facts(handle: str, row: ProductRow) -> tuple:
    out = []
    for col, label, unit in _COLUMNS:
        v = getattr(row, col)
        if v is None:
            continue
        out.append(FactItem(fact_id(handle, "col", col), label, _fmt(v), unit, "products"))
    return tuple(out)


def spec_facts(handle: str, specs: Sequence) -> tuple:
    return tuple(FactItem(fact_id(handle, "spec", s.spec_key), s.spec_name, s.spec_value or "", None,
                          "product_specs") for s in specs)


def feature_statuses(handle: str, results: dict, spec_keys: dict) -> tuple:
    out = []
    for fid, r in results.items():
        ids = []
        for ev in r.evidence:
            if ev.origin == "column":
                ids.append(fact_id(handle, "col", ev.name))
            elif ev.name in spec_keys:
                ids.append(fact_id(handle, "spec", spec_keys[ev.name]))
        out.append(FeatureStatus(fid, r.state.value, r.value, tuple(ids), r.data_quality))
    return tuple(out)


def constraint_statuses(plan: ResolvedPlan, row: ProductRow) -> tuple:
    """Per user constraint, is it satisfied by the live row? Compares live values only."""
    from .relaxation import describe

    out = []
    for key in plan.user_constraint_keys:
        want = getattr(plan.filters, key)
        if key == "resolution_class":
            allowed = {v for rc in want for v in RESOLUTION_VALUES[rc]}
            actual, ok = row.resolution, (None if row.resolution is None else row.resolution in allowed)
        elif isinstance(want, Range):
            actual = {"list_price": row.price}.get(key, getattr(row, key, None))
            ok = None if actual is None else ((want.min is None or actual >= want.min)
                                              and (want.max is None or actual <= want.max))
        elif isinstance(want, tuple):
            actual = getattr(row, key)
            ok = None if actual is None else actual in want
        else:
            actual = getattr(row, key)
            ok = actual == want
        out.append(ConstraintStatus(key, describe(key, plan.filters), None if actual is None else _fmt(actual), ok))
    return tuple(out)


def plan_feature_ids(plan: ResolvedPlan) -> list:
    ids = [*plan.required, *plan.preferred, *plan.attributes_asked,
           *[s.source for s in plan.numeric if s.source not in NUMERIC_COLUMN_SIGNALS]]
    return list(dict.fromkeys(ids))


def section_priority(plan: ResolvedPlan) -> list:
    """Sections relevant to the plan's mapped features, most-referenced first (ties: plan order)."""
    counts: dict = {}
    for f in plan_feature_ids(plan):
        s = FEATURES[f].section
        counts[s] = counts.get(s, 0) + 1
    return sorted(counts, key=lambda s: -counts[s])


# ---- LLM serialization / budget / confidence -------------------------------------------------

def _llm_product(p: ProductEvidence) -> dict:
    return {
        "handle": p.handle, "model_code": p.model_code, "name": p.name,
        "facts": [{"id": f.fact_id, "label": f.label, "value": f.value, **({"unit": f.unit} if f.unit else {})}
                  for f in p.facts],
        "features": [{"id": f.feature_id, "state": f.state, **({"value": f.value} if f.value is not None else {}),
                      "facts": list(f.fact_ids)} for f in p.features],
        "constraints": [{"constraint": c.requested, "actual": c.actual, "satisfied": c.satisfied}
                        for c in p.constraints],
        "passages": [{"section": x.section, "text": x.text} for x in p.passages],
        "selection": list(p.selection_reasons),
    }


def to_llm_payload(b: EvidenceBundle) -> dict:
    """Compact, deterministic evidence for a future answer LLM. Excludes similarity, fit scores,
    ranks, internal ids, canonical source ids, URLs, raw payload, descriptions and stale chunk
    price/availability (passages are already sanitized)."""
    return {
        "intent": b.intent, "confidence": b.retrieval_confidence, "request": b.plan_summary,
        "totals": {k: v for k, v in b.totals.items() if v not in (None, 0, False)},
        "products": [_llm_product(p) for p in b.products],
        "alternatives": [_llm_product(p) for p in b.alternatives],
        "gaps": [{"kind": g.kind, "detail": g.detail, **({"products": list(g.handles)} if g.handles else {})}
                 for g in b.gaps],
    }


def serialize_for_llm(b: EvidenceBundle) -> str:
    return json.dumps(to_llm_payload(b), ensure_ascii=False, separators=(",", ":"))


def approx_tokens(b: EvidenceBundle) -> int:
    """cl100k_base via the existing indexing helper (tiktoken when installed, else chars/3.2).
    An approximation of the eventual answer model's tokenizer."""
    return approx_token_count(serialize_for_llm(b))


def enforce_budget(b: EvidenceBundle, limit: int = TOKEN_BUDGET) -> EvidenceBundle:
    """Trim passages (lowest-ranked product first, last passage first) until within ``limit``.
    Products, facts, features, constraints and gaps are never trimmed."""
    trimmed = 0
    products = list(b.products)
    tokens = approx_tokens(b)
    while tokens > limit:
        idx = next((i for i in range(len(products) - 1, -1, -1) if products[i].passages), None)
        if idx is None:
            break
        products[idx] = replace(products[idx], passages=products[idx].passages[:-1])
        trimmed += 1
        b = replace(b, products=tuple(products))
        tokens = approx_tokens(b)
    return replace(b, budget={"approx_tokens": tokens, "limit": limit, "trimmed_passages": trimmed,
                              "over_budget": tokens > limit, "tokenizer": "cl100k_base (approx)"})


PARTIAL_GAPS = frozenset({"required_feature_not_listed", "attribute_not_listed_for_product",
                          "attribute_absence_unverified", "attribute_not_in_catalog", "not_in_catalog_domain",
                          "no_product_satisfies", "model_not_found", "semantic_unavailable",
                          "family_size_ambiguous"})


def classify_confidence(route: Route, has_products: bool, gaps: Sequence[Gap], non_discriminating: bool,
                        vector_only_need: bool) -> tuple:
    """Structural confidence. ``weak``: global fallback, nothing retrieved, or the structured
    evidence does not discriminate the shown products. ``partial``: some requested information is
    not listed / unverified / unavailable, or a free-text need is backed only by vector passages.
    ``strong``: otherwise. Never derived from similarity values."""
    if route in (Route.CLARIFY, Route.NO_RETRIEVAL):
        return "not_applicable", ("no_retrieval_route",)
    reasons = []
    if route is Route.SEMANTIC_FALLBACK:
        reasons.append("global_semantic_fallback")
    if not has_products:
        reasons.append("no_products_in_evidence")
    if non_discriminating:
        reasons.append("top_fit_band_exceeds_shortlist")
    if reasons:
        return "weak", tuple(reasons)
    partial = sorted({g.kind for g in gaps if g.kind in PARTIAL_GAPS})
    if vector_only_need:
        partial.append("free_text_need_backed_by_vector_passages_only")
    if partial:
        return "partial", tuple(partial)
    return "strong", ("constraints_and_requested_features_backed_by_catalog_facts",)


# ---- builder ----------------------------------------------------------------------------------------

@dataclass
class _Draft:
    row: ProductRow
    reasons: list
    passages: list = field(default_factory=list)
    extra_specs: list = field(default_factory=list)
    debug: dict = field(default_factory=dict)


def _plan_summary(plan: ResolvedPlan) -> dict:
    from .relaxation import describe

    return {"intent": plan.intent.value,
            "constraints": [describe(k, plan.filters) for k in plan.user_constraint_keys],
            "use_cases": list(plan.use_cases), "required_features": list(plan.required),
            "preferred_features": list(plan.preferred), "attributes_asked": list(plan.attributes_asked),
            "free_text_need": plan.free_text_need,
            "sort": None if plan.sort is None else f"{plan.sort.key.value} {plan.sort.direction.value}",
            "policies": list(plan.policy_notes)}


def _passage(c, row: ProductRow, retrieval: str) -> Passage:
    text, removed = sanitize_passage(c.content)
    return Passage(c.chunk_id, c.product_id, row.source, row.external_id, c.section, text, retrieval,
                   c.similarity, removed)


def _attach_section_passages(repo: CatalogRepository, drafts: list, sections: Sequence[str], per_product: int) -> None:
    if not drafts or not sections:
        return
    by_pid = {d.row.id: d for d in drafts}
    chunks = repo.chunks_by_section(list(by_pid), list(sections))
    order = {s: i for i, s in enumerate(sections)}
    for c in sorted(chunks, key=lambda c: (order[c.section], c.chunk_id)):
        d = by_pid[c.product_id]
        if len(d.passages) < per_product and all(p.section != c.section for p in d.passages):
            d.passages.append(_passage(c, d.row, "section_lookup"))


def _attach_vector_passages(drafts: list, hits: Sequence, per_product: int) -> None:
    by_pid = {d.row.id: d for d in drafts}
    for c in hits:                                   # hits are in similarity order
        d = by_pid.get(c.product_id)
        if d is not None and len(d.passages) < per_product and all(p.section != c.section for p in d.passages):
            d.passages.append(_passage(c, d.row, "vector"))


def build_evidence(plan: ResolvedPlan, route_plan: RoutePlan, result: StructuredResult,
                   repo: CatalogRepository, embedder=None, *, token_budget: int = TOKEN_BUDGET,
                   apply_semantic_tiebreak: bool = False) -> EvidenceBundle:
    """``apply_semantic_tiebreak`` is off by default: Phase 4C measured it slightly harmful on the
    Phase 3D cases (docs/PHASE_4C_EVIDENCE_SEMANTIC.md), so vector hits only supply passages and
    the accepted Phase 4B order stands unless a caller explicitly opts in."""
    route = route_plan.route
    gaps = [Gap(_FROM_4B.get(g.kind, g.kind), g.detail, g.kind, (), tuple(g.model_codes)) for g in result.gaps]
    semantic = {"path": None, "embedding_source": None, "embedding_lookups": 0, "candidate_ids": 0,
                "escaped_products": 0, "tiebreak": None}
    drafts: list = []
    alternatives: list = []
    feature_results = dict(result.feature_results)
    non_discriminating = False
    vector_only_need = False
    totals = {"excluded_unavailable": result.excluded_unavailable_count,
              "excluded_out_of_scope": result.excluded_out_of_scope_count,
              "matched": result.total_count, "truncated": result.truncated}

    def embed(text: str):
        if embedder is None:
            gaps.append(Gap("semantic_unavailable", "No query embedder configured; semantic passages skipped."))
            return None
        semantic["embedding_lookups"] += 1
        semantic["embedding_source"] = getattr(embedder, "source", type(embedder).__name__)
        try:
            return embedder.embed(text)
        except EmbeddingUnavailable:
            gaps.append(Gap("semantic_unavailable",
                            "No cached query embedding for this need; no new embedding is generated."))
            return None

    if route is Route.CLARIFY:
        fc = plan.family_comparison
        if fc is not None and fc.selected_size is None:
            gaps.append(Gap("family_size_ambiguous",
                            f"Families {list(fc.families)} share sizes {[_fmt(s) for s in fc.shared_sizes]}; "
                            f"status {fc.status}. A size must be chosen.", fc.status))

    elif route is Route.SQL_LOOKUP:
        drafts = [_Draft(r, ["named_product"]) for r in result.products[:MAX_NAMED_PRODUCTS]]
        if plan.intent is Intent.LOOKUP:
            _attach_section_passages(repo, drafts, ["overview"], 1)
        elif plan.intent is Intent.SPEC_QUESTION:
            _attach_section_passages(repo, drafts, section_priority(plan)[:1], 1)
            for a in plan.attributes_asked:
                names = FEATURES[a].spec_names
                if names and repo.spec_coverage(names) == 0:
                    gaps[:] = [g for g in gaps if not (g.kind == "attribute_not_listed_for_product"
                                                       and g.detail.startswith(f"{a}:"))]
                    gaps.append(Gap("attribute_not_in_catalog",
                                    f"{a}: no product in the catalog carries {list(names)}", a))

    elif route in (Route.SQL_FILTER, Route.SQL_AGGREGATE):
        limit = MAX_LIST_PRODUCTS
        drafts = [_Draft(r, ["sql_filter" if route is Route.SQL_FILTER else "extreme_value_tie_aware"])
                  for r in result.products[:limit]]
        if len(result.products) > limit:
            totals["not_shown"] = len(result.products) - limit

    elif route is Route.CONSTRAINT_FIRST:
        ranking, shortlist = result.ranking, result.shortlist
        candidate_ids = [p.id for p in result.candidates]
        semantic["candidate_ids"] = len(candidate_ids)
        hits = None
        if plan.free_text_need and candidate_ids:
            q = embed(plan.free_text_need)
            if q is not None:
                semantic["path"] = "candidate_scoped"
                hits = repo.vector_in_products(candidate_ids, q, NON_OVERVIEW_SECTIONS)
                escaped = {c.product_id for c in hits} - set(candidate_ids)
                semantic["escaped_products"] = len(escaped)
                if escaped:
                    raise AssertionError(f"vector search escaped the candidate set: {sorted(escaped)}")
                if apply_semantic_tiebreak:
                    structured_order = ranking.ranked_codes()
                    ranking, rep_q, _ = semantic_tiebreak(ranking, best_similarity_by_product(hits))
                    shortlist = compose_shortlist(ranking, choose_policy(plan.has_budget,
                                                                         bool(plan.preferred or plan.numeric)))
                    semantic["tiebreak"] = {"structured_order": structured_order,
                                            "final_order": ranking.ranked_codes(),
                                            "moved": [r.model_code for r in rep_q
                                                      if r.structured_rank != r.final_rank]}
                vector_only_need = True
        rank_of = {c.product.id: c for c in ranking.qualified}
        structured_rank = {c.product.id: c.rank for c in result.ranking.qualified}
        for c in shortlist.items:
            reasons = ["hard_constraints_met" if plan.user_constraint_keys else "no_hard_constraints"]
            if plan.scope_excluded_kinds:
                reasons.append("recommendation_scope_applied")
            tier = shortlist.tiers.get(c.product.model_code)
            if tier:
                reasons.append(f"price_tier={tier}")
            d = _Draft(c.product, reasons)
            d.debug = {"structured_rank": structured_rank.get(c.product.id), "final_rank": rank_of[c.product.id].rank,
                       "preferred_matched": c.preferred_matched, "preferred_total": c.preferred_total,
                       "numeric_signals": [list(s) for s in c.numeric_signals], "shortlist_policy": shortlist.policy}
            drafts.append(d)
        if hits is not None:
            _attach_vector_passages(drafts, hits, MAX_PASSAGES_PER_PRODUCT)
        else:
            _attach_section_passages(repo, drafts, section_priority(plan)[:MAX_PASSAGES_PER_PRODUCT],
                                     MAX_PASSAGES_PER_PRODUCT)
        band = _top_band(ranking.qualified)
        non_discriminating = bool(plan.preferred or plan.numeric) and len(band) > MAX_SHORTLIST
        totals.update(candidates=len(result.candidates), qualified=len(ranking.qualified),
                      required_not_listed=len(ranking.unknown), rejected=len(ranking.rejected),
                      top_fit_band=len(band), shown=len(shortlist.items))
        if ranking.unknown:
            gaps.append(Gap("required_feature_not_listed",
                            f"{len(ranking.unknown)} candidate(s) may be suitable but the catalog does not "
                            f"list required feature(s) {list(plan.required)}.",
                            "unknown_bucket", (), tuple(c.product.model_code for c in ranking.unknown[:3])))
        if not result.candidates or (plan.required and not ranking.qualified):
            probes = relax(plan, repo)
            totals["relaxation"] = [{"dropped": p.dropped, "requested": p.requested, "matches": p.matches}
                                    for p in probes]
            # A probe drops one constraint but may also return rows that satisfy it (always the case
            # when candidates exist and only a required feature failed). Label each row by the
            # constraints its live values actually violate; a row violating none is a candidate,
            # not a relaxation alternative (the unknown bucket below carries it if relevant).
            shown = set()
            for row, dropped in alternative_rows(probes):
                violated = [s.key for s in constraint_statuses(plan, row)
                            if s.satisfied is False or (s.satisfied is None and s.key in dropped)]
                if violated and row.id not in shown:
                    shown.add(row.id)
                    alternatives.append(_Draft(row, [f"violates:{k}" for k in violated]))
            for c in ranking.unknown[:3]:
                if c.product.id not in shown:
                    shown.add(c.product.id)
                    alternatives.append(_Draft(c.product, [f"required_not_listed:{','.join(plan.required)}"]))

    elif route is Route.PRODUCT_SCOPED_SEMANTIC:
        rows = list(plan.resolved_products[:MAX_NAMED_PRODUCTS])
        drafts = [_Draft(r, ["named_product"]) for r in rows]
        terms = probe_terms(plan.free_text_need or "")
        coverage = {t: catalog_coverage(t, repo.specs_mentioning(t)) for t in terms}
        all_specs = repo.get_specs([r.id for r in rows]) if rows else {}
        semantic["probe_terms"] = list(terms)
        for d in drafts:
            probe = lexical_probe(terms, all_specs.get(d.row.id, []), coverage)
            d.extra_specs = list(probe.matched_specs)
            d.debug["lexical_probe"] = probe.status
            if probe.status == "mentioned_in_specs":
                d.reasons.append(f"spec_mentions:{','.join(probe.matched_terms)}"
                                 + ("" if probe.all_terms_matched else f" (not all of {list(terms)})"))
            elif probe.status == "verified_not_listed":
                gaps.append(Gap("attribute_not_listed_for_product",
                                f"'{plan.free_text_need}': no spec row of this product mentions {list(terms)}, "
                                "while the catalog uses these terms elsewhere.", "lexical_probe", (),
                                (d.row.model_code,)))
            else:
                gaps.append(Gap("attribute_absence_unverified",
                                f"'{plan.free_text_need}': not found in this product's specs, and the catalog "
                                f"never uses {[t for t, n in coverage.items() if not n] or list(terms)}; "
                                "absence cannot be established.", probe.status, (), (d.row.model_code,)))
        q = embed(plan.free_text_need) if rows and plan.free_text_need else None
        if q is not None:
            semantic["path"] = "product_scoped"
            hits = repo.vector_in_products([r.id for r in rows], q, None)
            _attach_vector_passages(drafts, hits, PRODUCT_SCOPED_PASSAGES)

    elif route is Route.SEMANTIC_FALLBACK:
        q = embed(plan.free_text_need) if plan.free_text_need else None
        if q is not None:
            semantic["path"] = "global_fallback"
            hits = repo.vector_global(q, available_only=plan.filters.is_available)
            order = list(dict.fromkeys(c.product_id for c in hits))[:FALLBACK_PRODUCTS]
            excluded = {getattr(k, "value", k) for k in plan.filters.exclude_product_kinds}
            rows = [r for r in repo.get_products(order) if r.product_kind.value not in excluded]
            drafts = [_Draft(r, ["global_semantic_fallback"]) for r in rows[:MAX_PRODUCTS]]
            _attach_vector_passages(drafts, hits, MAX_PASSAGES_PER_PRODUCT)
            totals["fallback_window"] = len(order)

    # ---- facts, features, constraints, data quality ---------------------------------------------
    all_drafts = drafts + alternatives
    fids = plan_feature_ids(plan)
    names = spec_names_for(fids) if fids else ()
    specs = repo.get_specs([d.row.id for d in all_drafts], names) if names and all_drafts else {}
    if fids and all_drafts:
        missing = [d.row for d in all_drafts if d.row.id not in feature_results]
        if missing:
            from .features import evaluate_features

            feature_results.update(evaluate_features(missing, specs, fids))

    def finish(prefix: str, items: list) -> list:
        out = []
        for i, d in enumerate(items, start=1):
            h = f"{prefix}{i}"
            rows = list(specs.get(d.row.id, [])) + [s for s in d.extra_specs
                                                    if s.spec_key not in {x.spec_key for x in specs.get(d.row.id, [])}]
            keys = {s.spec_name: s.spec_key for s in rows}
            fr = {f: r for f, r in feature_results.get(d.row.id, {}).items() if f in fids}
            out.append(ProductEvidence(
                h, d.row.id, d.row.source, d.row.external_id, d.row.model_code, d.row.name, d.row.product_url,
                column_facts(h, d.row) + spec_facts(h, rows), feature_statuses(h, fr, keys),
                constraint_statuses(plan, d.row), tuple(d.passages), tuple(d.reasons), d.debug))
            for f, r in fr.items():
                if r.data_quality:
                    gaps.append(Gap("data_quality", f"{f}: {r.data_quality} (raw value not used)", r.data_quality,
                                    (h,), (d.row.model_code,)))
        return out

    products = finish("P", drafts)
    alts = finish("A", alternatives)
    handle_of = {p.model_code: p.handle for p in (*products, *alts)}
    if result.excluded_unavailable_count:
        gaps.append(Gap("excluded_unavailable",
                        f"{result.excluded_unavailable_count} matching product(s) are unavailable and not shown."))
    if result.excluded_out_of_scope_count:
        gaps.append(Gap("excluded_out_of_scope",
                        f"{result.excluded_out_of_scope_count} matching special-purpose display product(s) are "
                        "outside the default recommendation scope."))
    gaps = [replace(g, handles=g.handles or tuple(handle_of[m] for m in g.model_codes if m in handle_of))
            for g in gaps]

    level, reasons = classify_confidence(route, bool(products), gaps, non_discriminating, vector_only_need)
    bundle = EvidenceBundle(EVIDENCE_VERSION, plan.intent.value, route.value, route_plan.rule, _plan_summary(plan),
                            tuple(products), tuple(alts), tuple(gaps), totals, level, reasons, semantic, {})
    return enforce_budget(bundle, token_budget)

