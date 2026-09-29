"""Executes the structured routes of a ``RoutePlan`` (Phase 4B).

SQL_LOOKUP, SQL_FILTER, SQL_AGGREGATE and CONSTRAINT_FIRST run against ``CatalogRepository``.
CLARIFY / NO_RETRIEVAL need no retrieval; PRODUCT_SCOPED_SEMANTIC / SEMANTIC_FALLBACK are
returned with ``executed=False`` -- vector execution is Phase 4C.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from .catalog_repository import MAX_LIST_LIMIT, CatalogRepository
from .features import NUMERIC_COLUMN_SIGNALS, evaluate_features, spec_names_for
from .ranking import choose_policy, compose_shortlist, rank_candidates
from .schemas import FeatureState, Filters, Gap, RankingResult, ResolvedPlan, Route, RoutePlan, Shortlist

DEFAULT_LIST_LIMIT = 20


@dataclass(frozen=True)
class StructuredResult:
    route: RoutePlan
    executed: bool
    products: tuple = ()                   # ProductRow: lookup / list / aggregate / compare rows
    total_count: Optional[int] = None
    truncated: bool = False
    candidates: tuple = ()                 # ProductRow admitted by the filters (CONSTRAINT_FIRST)
    feature_results: dict = field(default_factory=dict)   # product_id -> {feature_id: FeatureResult}
    ranking: Optional[RankingResult] = None
    shortlist: Optional[Shortlist] = None
    excluded_unavailable_count: int = 0
    excluded_out_of_scope_count: int = 0
    relaxation: tuple = ()                 # (constraint key, count if that constraint is dropped)
    gaps: tuple = ()

    def ranked_codes(self) -> list:
        if self.ranking is not None:
            return self.ranking.ranked_codes()
        return [p.model_code for p in self.products]


def _count(repo: CatalogRepository, f: Filters) -> int:
    return repo.count(f)[0][1]


def _policy_exclusions(repo: CatalogRepository, plan: ResolvedPlan) -> tuple:
    """How many matches the availability default / scope policy hid (reported, never silent)."""
    f = plan.filters
    unavailable = _count(repo, replace(f, is_available=False)) if plan.availability_default_applied else 0
    out_of_scope = 0
    if plan.scope_excluded_kinds:
        out_of_scope = _count(repo, replace(f, exclude_product_kinds=())) - _count(repo, f)
    return unavailable, out_of_scope


def _relaxation(repo: CatalogRepository, plan: ResolvedPlan) -> tuple:
    empty = Filters()
    return tuple((key, _count(repo, replace(plan.filters, **{key: getattr(empty, key)})))
                 for key in plan.user_constraint_keys)


def _year_gaps(plan: ResolvedPlan, products) -> list:
    if plan.stated_year is None:
        return []
    named = [p.model_code for p in products if str(plan.stated_year) in (p.name or "")]
    if not named:
        return []
    return [Gap("stated_year_in_name_only",
                f"Name mentions {plan.stated_year}; the catalog year column says "
                f"{sorted({p.year for p in products if p.model_code in named})}.", tuple(named))]


def execute(plan: ResolvedPlan, route_plan: RoutePlan, repo: CatalogRepository) -> StructuredResult:
    r = route_plan.route
    gaps = list(plan.gaps)
    if r in (Route.CLARIFY, Route.NO_RETRIEVAL):
        return StructuredResult(route_plan, True, gaps=tuple(gaps))
    if not route_plan.executable_in_4b:
        return StructuredResult(route_plan, False, gaps=tuple(gaps))

    if r is Route.SQL_LOOKUP:
        fc = plan.family_comparison
        products = fc.products if fc is not None else plan.resolved_products
        results = {}
        if plan.attributes_asked and products:
            specs = repo.get_specs([p.id for p in products], spec_names_for(plan.attributes_asked))
            results = evaluate_features(products, specs, plan.attributes_asked)
            for p in products:
                for fid, fr in results[p.id].items():
                    if fr.state is FeatureState.NOT_LISTED:
                        gaps.append(Gap("attribute_not_listed_for_product",
                                        f"{fid}: not listed in the catalog"
                                        + (f" ({fr.data_quality})" if fr.data_quality else ""),
                                        (p.model_code,)))
        return StructuredResult(route_plan, True, products=tuple(products), total_count=len(products),
                                feature_results=results, gaps=tuple(gaps + _year_gaps(plan, products)))

    unavailable, out_of_scope = _policy_exclusions(repo, plan)

    if r is Route.SQL_AGGREGATE:
        if plan.limit in (None, 1):
            rows = repo.extreme(plan.filters, plan.sort.key, plan.sort.direction)
            total, truncated = len(rows), False
        else:
            sr = repo.search(plan.filters, (plan.sort.key, plan.sort.direction), plan.limit, MAX_LIST_LIMIT)
            rows, total, truncated = sr.rows, sr.total_count, sr.truncated
            gaps.append(Gap("top_n_ties_not_expanded", "Top-N ordering: ties at the boundary may be cut."))
        if not rows:
            gaps.append(Gap("no_product_satisfies", "No product matches the constraints."))
        return StructuredResult(route_plan, True, products=tuple(rows), total_count=total, truncated=truncated,
                                excluded_unavailable_count=unavailable, excluded_out_of_scope_count=out_of_scope,
                                relaxation=_relaxation(repo, plan) if not rows else (),
                                gaps=tuple(gaps + _year_gaps(plan, rows)))

    if r is Route.SQL_FILTER:
        sort = (plan.sort.key, plan.sort.direction) if plan.sort else None
        sr = repo.search(plan.filters, sort, plan.limit or DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT)
        if not sr.rows:
            gaps.append(Gap("no_product_satisfies", "No product matches the constraints."))
        return StructuredResult(route_plan, True, products=tuple(sr.rows), total_count=sr.total_count,
                                truncated=sr.truncated, excluded_unavailable_count=unavailable,
                                excluded_out_of_scope_count=out_of_scope,
                                relaxation=_relaxation(repo, plan) if not sr.rows else (),
                                gaps=tuple(gaps + _year_gaps(plan, sr.rows)))

    # CONSTRAINT_FIRST
    cand = repo.candidates(plan.filters)
    if cand.truncated:
        gaps.append(Gap("candidates_truncated", f"Candidate set capped at {cand.limit}."))
    fids = [*plan.required, *plan.preferred,
            *[s.source for s in plan.numeric if s.source not in NUMERIC_COLUMN_SIGNALS]]
    fids = list(dict.fromkeys(fids))
    specs = repo.get_specs([p.id for p in cand.rows], spec_names_for(fids)) if fids and cand.rows else {}
    results = evaluate_features(cand.rows, specs, fids) if fids else {p.id: {} for p in cand.rows}
    ranking = rank_candidates(cand.rows, results, plan.required, plan.preferred, plan.numeric)
    policy = choose_policy(plan.has_budget, bool(plan.preferred or plan.numeric))
    shortlist = compose_shortlist(ranking, policy)
    if not cand.rows:
        gaps.append(Gap("no_product_satisfies", "No product matches the hard constraints."))
    elif plan.required and not ranking.qualified:
        gaps.append(Gap("required_feature_not_confirmed",
                        "No candidate is confirmed to have every required feature; "
                        f"{len(ranking.unknown)} possibly suitable (not listed in the catalog)."))
    return StructuredResult(route_plan, True, total_count=cand.total_count, truncated=cand.truncated,
                            candidates=tuple(cand.rows), feature_results=results, ranking=ranking,
                            shortlist=shortlist, excluded_unavailable_count=unavailable,
                            excluded_out_of_scope_count=out_of_scope,
                            relaxation=_relaxation(repo, plan) if not cand.rows else (),
                            gaps=tuple(gaps + _year_gaps(plan, cand.rows)))


def run(delta, repo: CatalogRepository, vocab=None):
    """Convenience: ``QueryPlanDelta -> (ResolvedPlan, RoutePlan, StructuredResult)``."""
    from .planning import resolve_plan
    from .router import route

    plan = resolve_plan(delta, repo, vocab)
    rp = route(plan)
    return plan, rp, execute(plan, rp, repo)
