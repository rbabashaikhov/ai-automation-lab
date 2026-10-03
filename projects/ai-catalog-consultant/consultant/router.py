"""Pure deterministic router: ``ResolvedPlan -> RoutePlan`` (Phase 4A §8, top-down).

Invariant: a plan that asks for a deterministic ordering (``sort``, i.e. cheapest / most
expensive / largest / smallest / highest Hz) can never reach a semantic route -- the ordering
rule fires before any rule that could select vector retrieval.
"""

from __future__ import annotations

from .features import FEATURES
from .schemas import STRUCTURED_ROUTES, ContextMode, Intent, ResolvedPlan, Route, RoutePlan


def _rp(route: Route, rule: str) -> RoutePlan:
    return RoutePlan(route, rule, route in STRUCTURED_ROUTES)


def route(plan: ResolvedPlan) -> RoutePlan:
    if plan.intent is Intent.NON_RETRIEVAL:
        return _rp(Route.NO_RETRIEVAL, "R1-non-retrieval")
    if plan.intent is Intent.CLARIFY or plan.clarification_reason:
        return _rp(Route.CLARIFY, "R1-clarify")
    if plan.context_mode is not ContextMode.NEW:
        return _rp(Route.CLARIFY, "R1-needs-conversation-state")
    if plan.intent is Intent.COMPARE:
        fc = plan.family_comparison
        if fc is not None and fc.status != "resolved_user_size" and fc.status != "resolved_single_shared_size":
            return _rp(Route.CLARIFY, "R2-family-size-ambiguous")
        return _rp(Route.SQL_LOOKUP, "R2-compare")
    if plan.intent is Intent.LOOKUP:
        return _rp(Route.SQL_LOOKUP, "R2-lookup")
    if plan.intent is Intent.SPEC_QUESTION:
        if plan.attributes_asked and not plan.free_text_need and all(a in FEATURES for a in plan.attributes_asked):
            return _rp(Route.SQL_LOOKUP, "R3-registry-attribute")
        return _rp(Route.PRODUCT_SCOPED_SEMANTIC, "R3-long-tail-attribute")
    # --- ordering invariant: everything below this line with a sort is structured ----------
    if plan.intent is Intent.SUPERLATIVE or (plan.sort is not None and plan.limit is not None):
        if plan.sort is None:
            return _rp(Route.CLARIFY, "R4-superlative-without-sort")
        return _rp(Route.SQL_AGGREGATE, "R4-ordering-with-limit")
    if plan.intent is Intent.LIST or plan.sort is not None:
        return _rp(Route.SQL_FILTER, "R5-list-or-ordering")
    if plan.intent is Intent.RECOMMEND:
        if plan.user_constraint_keys or plan.use_cases or plan.required or plan.preferred or plan.numeric:
            return _rp(Route.CONSTRAINT_FIRST, "R6-constraint-first")
        if plan.free_text_need:
            return _rp(Route.SEMANTIC_FALLBACK, "R7-unmapped-need-only")
        return _rp(Route.CLARIFY, "R8-too-vague")
    return _rp(Route.CLARIFY, "R9-unhandled")
