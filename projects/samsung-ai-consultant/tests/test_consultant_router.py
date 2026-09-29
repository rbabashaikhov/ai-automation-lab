"""Pure router: Phase 4A §8 rules and the ordering invariant."""

import itertools

import pytest

from consultant.router import route
from consultant.schemas import (
    SEMANTIC_ROUTES, ContextMode, FamilyComparison, Filters, Intent, NumericSignal, Range, ResolvedPlan,
    Route, Sort, SortDir, SortKey,
)


def plan(intent, **kw):
    base = dict(intent=intent, context_mode=ContextMode.NEW, filters=Filters(), user_constraint_keys=(),
                sort=None, limit=None, resolutions=(), family_comparison=None, use_cases=(), required=(),
                preferred=(), numeric=(), attributes_asked=(), free_text_need=None, stated_year=None,
                ordinal_refs=(), clarification_reason=None, gaps=(), policy_notes=())
    base.update(kw)
    return ResolvedPlan(**base)


CHEAPEST = Sort(SortKey.EFFECTIVE_PRICE, SortDir.ASC)


@pytest.mark.parametrize("p,expected", [
    (plan(Intent.NON_RETRIEVAL), Route.NO_RETRIEVAL),
    (plan(Intent.CLARIFY), Route.CLARIFY),
    (plan(Intent.RECOMMEND, context_mode=ContextMode.REFINE), Route.CLARIFY),
    (plan(Intent.COMPARE, context_mode=ContextMode.REFERENCE, ordinal_refs=(1, 2)), Route.CLARIFY),
    (plan(Intent.LOOKUP), Route.SQL_LOOKUP),
    (plan(Intent.COMPARE), Route.SQL_LOOKUP),
    (plan(Intent.SPEC_QUESTION, attributes_asked=("hdmi_2_1",)), Route.SQL_LOOKUP),
    (plan(Intent.SPEC_QUESTION, free_text_need="Bixby"), Route.PRODUCT_SCOPED_SEMANTIC),
    (plan(Intent.SUPERLATIVE, sort=CHEAPEST, limit=1), Route.SQL_AGGREGATE),
    (plan(Intent.SUPERLATIVE), Route.CLARIFY),
    (plan(Intent.LIST, user_constraint_keys=("screen_size_inches",)), Route.SQL_FILTER),
    (plan(Intent.RECOMMEND, use_cases=("gaming",), preferred=("vrr",)), Route.CONSTRAINT_FIRST),
    (plan(Intent.RECOMMEND, user_constraint_keys=("screen_size_inches",)), Route.CONSTRAINT_FIRST),
    (plan(Intent.RECOMMEND, numeric=(NumericSignal("screen_size_inches", SortDir.ASC),)), Route.CONSTRAINT_FIRST),
    (plan(Intent.RECOMMEND, free_text_need="уютный"), Route.SEMANTIC_FALLBACK),
    (plan(Intent.RECOMMEND), Route.CLARIFY),
])
def test_routing_matrix(p, expected):
    assert route(p).route is expected


def test_family_compare_ambiguity_routes_to_clarify():
    fc = FamilyComparison(("S95H", "S90H"), "ambiguous_multiple_shared_sizes", (55.0, 65.0), None)
    assert route(plan(Intent.COMPARE, family_comparison=fc)).route is Route.CLARIFY
    ok = FamilyComparison(("S95H", "S90H"), "resolved_user_size", (55.0, 65.0), 65.0)
    assert route(plan(Intent.COMPARE, family_comparison=ok)).route is Route.SQL_LOOKUP


@pytest.mark.parametrize("key,direction", itertools.product(list(SortKey), list(SortDir)))
@pytest.mark.parametrize("intent", [Intent.SUPERLATIVE, Intent.RECOMMEND, Intent.LIST])
@pytest.mark.parametrize("need", [None, "что-нибудь уютное"])
@pytest.mark.parametrize("limit", [None, 1, 3])
def test_ordering_never_routes_to_semantic(key, direction, intent, need, limit):
    """cheapest / most expensive / largest / smallest / highest Hz -> never vector ranking."""
    r = route(plan(intent, sort=Sort(key, direction), limit=limit, free_text_need=need))
    assert r.route not in SEMANTIC_ROUTES
    assert r.route in (Route.SQL_AGGREGATE, Route.SQL_FILTER)


def test_route_plan_marks_semantic_routes_non_executable():
    assert route(plan(Intent.RECOMMEND, free_text_need="x")).executable_in_4b is False
    assert route(plan(Intent.LIST)).executable_in_4b is True
    assert route(plan(Intent.LOOKUP)).rule == "R2-lookup"


def test_router_is_pure():
    p = plan(Intent.RECOMMEND, user_constraint_keys=("effective_price",),
             filters=Filters(effective_price=Range(None, 1.0)))
    assert route(p) == route(p)
