"""Plan contract validation: closed vocabularies must actually be closed."""

import pytest

from consultant.schemas import (
    Filters, Intent, PlanValidationError, QueryPlanDelta, Range, RefKind, Sort, SortDir, SortKey, Strength,
)
from evaluation.dataset import load_dataset
from evaluation.run_consultant_structured import load_gold_plans


def test_minimal_plan_defaults():
    p = QueryPlanDelta.from_dict({"intent": "recommend"})
    assert p.intent is Intent.RECOMMEND and p.constraints == Filters() and p.sort is None


def test_full_plan_parses():
    p = QueryPlanDelta.from_dict({
        "intent": "recommend", "context_mode": "new",
        "model_refs": [{"text": "S95H", "kind": "family"}],
        "constraints": {"panel_technology": ["OLED"], "screen_size_inches": 65,
                        "effective_price": {"max": 200000}, "refresh_rate_hz": {"min": 120},
                        "resolution_class": ["4K"], "is_available": True},
        "use_cases": ["gaming"], "features": [{"id": "vrr", "strength": "required"}],
        "sort": {"key": "effective_price", "dir": "asc"}, "limit": 1})
    assert p.model_refs[0].kind is RefKind.FAMILY
    assert p.constraints.screen_size_inches == Range(65.0, 65.0)
    assert p.constraints.effective_price == Range(None, 200000.0)
    assert p.features[0].strength is Strength.REQUIRED
    assert p.sort == Sort(SortKey.EFFECTIVE_PRICE, SortDir.ASC)
    assert p.constraints.user_constraint_keys() == (
        "panel_technology", "resolution_class", "screen_size_inches", "effective_price",
        "refresh_rate_hz", "is_available")


@pytest.mark.parametrize("raw", [
    {"intent": "buy_now"},                                             # unknown intent
    {"intent": "list", "bogus": 1},                                    # unknown top-level key
    {"intent": "list", "constraints": {"price": {"max": 1}}},          # unknown filter key
    {"intent": "list", "constraints": {"p.price; DROP TABLE products": 1}},
    {"intent": "list", "sort": {"key": "name", "dir": "asc"}},         # unknown sort key
    {"intent": "list", "sort": {"key": "effective_price", "dir": "up"}},
    {"intent": "list", "constraints": {"screen_size_inches": {"min": 80, "max": 60}}},
    {"intent": "list", "constraints": {"screen_size_inches": {}}},
    {"intent": "list", "constraints": {"effective_price": {"max": -1}}},
    {"intent": "list", "constraints": {"resolution_class": ["8K"]}},
    {"intent": "list", "constraints": {"is_available": "yes"}},
    {"intent": "list", "limit": 0},
    {"intent": "lookup", "model_refs": [{"text": "", "kind": "full"}]},
    {"intent": "lookup", "model_refs": [{"text": "X", "kind": "series"}]},
    {"intent": "recommend", "features": [{"id": "vrr", "strength": "must"}]},
    {"intent": "compare", "ordinal_refs": [0]},
])
def test_invalid_plans_fail_safely(raw):
    with pytest.raises(PlanValidationError):
        QueryPlanDelta.from_dict(raw)


def test_enums_are_closed():
    for enum_cls, bad in ((SortKey, "name"), (SortDir, "up"), (Intent, "buy")):
        with pytest.raises(ValueError):
            enum_cls(bad)


def test_every_phase3d_case_has_a_valid_gold_plan():
    plans = load_gold_plans()
    assert set(plans) == {c.id for c in load_dataset()}
