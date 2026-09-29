"""Structured plan -> route -> repository -> features -> ranking -> shortlist, end to end.

Disposable DB only (tests/run_db_tests.sh). No network, no OpenAI, no production access.
These are the Phase 4B Consultant cases that live outside the unchanged Phase 3D dataset.
"""

import pytest

from consultant.catalog_repository import CatalogRepository
from consultant.retrieval import run
from consultant.schemas import FeatureState, PlanValidationError, QueryPlanDelta, Route
from evaluation.dataset import load_dataset
from evaluation.run_consultant_structured import evaluate, load_gold_plans

from .consultant_fixtures import seed
from .db_fixtures import db_conn, requires_db  # noqa: F401

pytestmark = requires_db


@pytest.fixture
def repo(db_conn):  # noqa: F811
    seed(db_conn)
    return CatalogRepository(db_conn)


def go(repo, raw):
    return run(QueryPlanDelta.from_dict(raw), repo)


def codes(rows):
    return [p.model_code for p in rows]


def gap_kinds(res):
    return [g.kind for g in res.gaps]


def test_exact_full_model_lookup(repo):
    plan, rp, res = go(repo, {"intent": "lookup", "model_refs": [{"text": "QE65S95HAUXPY", "kind": "full"}]})
    assert rp.route is Route.SQL_LOOKUP and codes(res.products) == ["QE65S95HAUXPY"]
    assert res.products[0].identity[0] == "galaxystore"


def test_unknown_model_is_reported_with_suggestions(repo):
    plan, rp, res = go(repo, {"intent": "lookup", "model_refs": [{"text": "QE55S90HAEXPY", "kind": "full"}]})
    assert res.products == ()
    (gap,) = [g for g in res.gaps if g.kind == "model_not_found"]
    assert gap.model_codes[0] == "QE65S90HAEXPY"     # edit distance 1: nearest catalog code, never silent
    assert all(len(c) == 13 for c in gap.model_codes) and len(gap.model_codes) <= 3


def test_family_resolution(repo):
    plan, rp, res = go(repo, {"intent": "lookup", "model_refs": [{"text": "S95H", "kind": "family"}]})
    assert sorted(codes(res.products)) == ["QE55S95HAUXPY", "QE65S95HAUXPY", "QE77S95HAEXPY", "QE83S95HAEXPY"]


def test_family_comparison_ambiguity_is_not_resolved_silently(repo):
    plan, rp, res = go(repo, {"intent": "compare", "model_refs": [{"text": "S95H", "kind": "family"},
                                                                   {"text": "S90H", "kind": "family"}]})
    fc = plan.family_comparison
    assert rp.route is Route.CLARIFY and fc.status == "ambiguous_multiple_shared_sizes"
    assert fc.selected_size is None and fc.shared_sizes == (65.0, 83.0) and res.products == ()


def test_family_comparison_with_user_size(repo):
    plan, rp, res = go(repo, {"intent": "compare", "compare_size_inches": 65,
                              "model_refs": [{"text": "S95H", "kind": "family"}, {"text": "S90H", "kind": "family"}]})
    assert rp.route is Route.SQL_LOOKUP and codes(res.products) == ["QE65S95HAUXPY", "QE65S90HAEXPY"]


def _spec_answer(repo, code, attr):
    plan, rp, res = go(repo, {"intent": "spec_question", "model_refs": [{"text": code, "kind": "full"}],
                              "attributes_asked": [attr]})
    assert rp.route is Route.SQL_LOOKUP
    return res.feature_results[res.products[0].id][attr], res


def test_hdmi_2_1_yes(repo):
    fr, _ = _spec_answer(repo, "MNA114MS1CCXRU", "hdmi_2_1")
    assert fr.state is FeatureState.YES and fr.evidence[0].value == "2.1"


def test_hdmi_2_1_not_listed_is_a_gap_not_a_no(repo):
    fr, res = _spec_answer(repo, "QE65S95HAUXPY", "hdmi_2_1")
    assert fr.state is FeatureState.NOT_LISTED
    assert "attribute_not_listed_for_product" in gap_kinds(res)
    # hdmi_2_1 == 'no' needs an explicit lower version, absent from production: see test_consultant_features


def test_long_tail_spec_question_routes_to_semantic_and_is_not_executed(repo):
    plan, rp, res = go(repo, {"intent": "spec_question", "model_refs": [{"text": "QE65S95HAUXPY", "kind": "full"}],
                              "free_text_need": "Bixby"})
    assert rp.route is Route.PRODUCT_SCOPED_SEMANTIC and res.executed is False


def test_vrr_required_keeps_unknown_separately(repo):
    plan, rp, res = go(repo, {"intent": "recommend", "features": [{"id": "vrr", "strength": "required"}]})
    r = res.ranking
    assert rp.route is Route.CONSTRAINT_FIRST and r.qualified and r.unknown and not r.rejected
    assert all(c.required["vrr"] is FeatureState.YES for c in r.qualified)
    assert all(c.required["vrr"] is FeatureState.NOT_LISTED for c in r.unknown)


@pytest.mark.parametrize("fid", ["allm", "dolby_atmos", "anti_glare"])
def test_preferred_feature_ranks_confirmed_products_first(repo, fid):
    plan, rp, res = go(repo, {"intent": "recommend", "features": [{"id": fid, "strength": "preferred"}]})
    states = [c.features[fid].state for c in res.ranking.qualified]
    first_other = next((i for i, s in enumerate(states) if s is not FeatureState.YES), len(states))
    assert all(s is not FeatureState.YES for s in states[first_other:])


def test_sound_power_and_recommendation_scope(repo):
    plan, rp, res = go(repo, {"intent": "recommend", "use_cases": ["sound"]})
    top = res.ranking.qualified[0]
    assert top.numeric_signals == (("sound_power_w", 70.0),)
    assert "MNA114MS1CCXRU" not in res.ranking.ranked_codes()           # 120 W professional display
    assert res.excluded_out_of_scope_count == 2
    plan, rp, res = go(repo, {"intent": "recommend", "use_cases": ["sound"], "include_special_products": True})
    special = {c.product.model_code: c for c in res.ranking.qualified}["MNA114MS1CCXRU"]
    assert res.excluded_out_of_scope_count == 0
    # 120 W, but its formats list only 'Dolby Digital' -> dolby_atmos not_listed -> preferred 0/1,
    # so ranking v1 (preferred count before numeric signal) places it after 70 W Atmos products.
    assert special.numeric_signals == (("sound_power_w", 120.0),) and special.preferred_matched == 0
    assert special.rank > top.rank


def test_special_product_exact_lookup_still_works(repo):
    for code in ("MNA114MS1CCXRU", "UE27LSM7FAXXPY"):
        plan, rp, res = go(repo, {"intent": "lookup", "model_refs": [{"text": code, "kind": "full"}]})
        assert codes(res.products) == [code]


def test_bright_room_preserves_brightness_gap(repo):
    plan, rp, res = go(repo, {"intent": "recommend", "use_cases": ["bright_room"]})
    assert "brightness_not_in_catalog" in gap_kinds(res)
    assert res.ranking.qualified[0].features["anti_glare"].state is FeatureState.YES


def test_malformed_dimensions_are_never_guessed(repo):
    plan, rp, res = go(repo, {"intent": "recommend", "use_cases": ["thin_wall"]})
    by_code = {c.product.model_code: c for c in (*res.ranking.qualified, *res.ranking.unknown)}
    bad = by_code["QE55LS03HAUXPY"].features["depth_cm"]
    assert bad.value is None and bad.data_quality == "malformed_component"
    assert by_code["QE65QN80HAUXPY"].features["depth_cm"].data_quality == "implausible_depth"
    ranked = res.ranking.ranked_codes()
    assert ranked.index("QE65S95HAUXPY") < ranked.index("QE55LS03HAUXPY")   # unknown depth sorts after known


def test_effective_price_budget_vs_explicit_list_price(repo):
    plan, rp, res = go(repo, {"intent": "list", "constraints": {"panel_technology": ["Neo QLED"],
                                                                "effective_price": {"max": 200000}}})
    assert "QE75QN80HAUXPY" in codes(res.products)
    plan, rp, res = go(repo, {"intent": "list", "constraints": {"panel_technology": ["Neo QLED"],
                                                                "list_price": {"max": 200000}}})
    assert "QE75QN80HAUXPY" not in codes(res.products)


def test_cheapest_oled_is_tie_aware_sql(repo):
    plan, rp, res = go(repo, {"intent": "superlative", "constraints": {"panel_technology": ["OLED"]},
                              "sort": {"key": "effective_price", "dir": "asc"}, "limit": 1})
    assert rp.route is Route.SQL_AGGREGATE
    assert codes(res.products) == ["QE42S90HAEXPY"]
    plan, rp, res = go(repo, {"intent": "superlative", "constraints": {"panel_technology": ["OLED"]},
                              "sort": {"key": "screen_size_inches", "dir": "desc"}, "limit": 1})
    assert sorted(codes(res.products)) == ["QE83S85HAEXPY", "QE83S90HAEXPY", "QE83S95HAEXPY"]


def test_75_inch_listing_reports_hidden_unavailable(repo):
    plan, rp, res = go(repo, {"intent": "list", "constraints": {"screen_size_inches": 75}})
    assert rp.route is Route.SQL_FILTER
    assert all(p.screen_size_inches == 75 and p.is_available for p in res.products)
    assert len(res.products) == 6 and res.excluded_unavailable_count == 0
    assert "availability_default=available_only" in plan.policy_notes


def test_no_result_hard_constraints_offer_relaxation(repo):
    plan, rp, res = go(repo, {"intent": "recommend", "constraints": {"panel_technology": ["OLED"],
                                                                     "effective_price": {"max": 30000}}})
    assert res.candidates == () and "no_product_satisfies" in gap_kinds(res)
    relax = dict(res.relaxation)
    assert relax["effective_price"] > 0 and relax["panel_technology"] > 0


def test_ranking_and_shortlist_are_separate(repo):
    plan, rp, res = go(repo, {"intent": "recommend", "use_cases": ["gaming"]})
    order = res.ranking.ranked_codes()
    short = [c.product.model_code for c in res.shortlist.items]
    assert len(short) <= 8 and [order.index(c) for c in short] == sorted(order.index(c) for c in short)
    assert res.ranking.qualified[0].rank == 1


def test_stale_year_is_surfaced_not_filtered(repo):
    plan, rp, res = go(repo, {"intent": "list", "constraints": {"category": ["The Frame"]}, "stated_year": 2023})
    kinds = gap_kinds(res)
    assert "year_not_in_catalog" in kinds and "stated_year_in_name_only" in kinds
    assert "QE32LS03CBUXRU" in codes(res.products)


def test_unknown_registry_ids_and_catalog_values_fail_safely(repo):
    for raw in ({"intent": "recommend", "use_cases": ["kitchen_party"]},
                {"intent": "recommend", "features": [{"id": "brightness_nits"}]},
                {"intent": "list", "constraints": {"panel_technology": ["QD-OLED"]}}):
        with pytest.raises(PlanValidationError):
            go(repo, raw)


def test_phase3d_gold_plans_run_structurally(repo):
    records, results = evaluate(repo, load_dataset(), load_gold_plans())
    routes = {r["id"]: r["route"] for r in records}
    assert routes["difficult-cheapest-superlative"] == "SQL_AGGREGATE"
    assert routes["structured-largest-oled"] == "SQL_AGGREGATE"
    assert routes["difficult-budget-vs-large-tradeoff"] == "SEMANTIC_FALLBACK"
    assert all(r["route"] not in ("PRODUCT_SCOPED_SEMANTIC", "SEMANTIC_FALLBACK")
               for r in records if r["family"] == "aggregate_not_retrieval")
