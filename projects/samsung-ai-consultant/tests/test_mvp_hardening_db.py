"""Phase 4F.3 MVP hardening, end to end on a disposable PostgreSQL+pgvector container (tests/run_db_tests.sh):
real migrations, the curated 31-product fixture, the unchanged pipeline. No network, no OpenAI, no production
access. Fixture facts used below: 31 products, 30 available; Dolby Atmos is listed for 22 (21 available)."""

import pytest

from consultant.agent_tools import SUMMARY_FEATURE_IDS, ConsultantTools
from consultant.catalog_repository import CatalogRepository
from consultant.features import FEATURES, evaluate_features
from consultant.schemas import Filters
from consultant.semantic_guard import MessageEvidence

from .consultant_fixtures import product_rows, seed, seed_chunks
from .db_fixtures import db_conn, requires_db  # noqa: F401

pytestmark = requires_db

PS5 = "Посоветуйте телевизор под PS5, сын в основном в шутеры играет."


@pytest.fixture
def env(db_conn):  # noqa: F811
    ids = seed(db_conn)
    seed_chunks(db_conn, ids)
    repo = CatalogRepository(db_conn)
    return ConsultantTools.for_repository(repo), repo


def conversation(*messages):
    return tuple(MessageEvidence.from_text(m) for m in messages)


def codes(payload, key="products"):
    return [p["model_code"] for p in payload.get(key, [])]


def expected_states(feature: str, available=None) -> dict:
    """The registry's own evaluation over the fixture: the independent oracle for a feature count."""
    rows, specs, _ = product_rows()
    rows = [r for r in rows if available is None or r.is_available == available]
    states = [evaluate_features([r], specs, [feature])[r.id][feature].state.value for r in rows]
    return {s: states.count(s) for s in ("yes", "no", "not_listed")}


# ---- MVP-1 ------------------------------------------------------------------------------------------------

def test_ps5_recommendation_shows_the_unapplied_requirement_as_unknown(env):
    """4F.2 PA-02 turn 1 on the real pipeline: same products and order as the plain gaming recommendation, and the
    HDMI 2.1 the Agent wanted is `not_listed` on every one of them instead of missing."""
    tools, _ = env
    plain = tools.call("recommend_tvs", {"use_cases": ["gaming"], "preferred_features": ["allm", "vrr"]})
    guarded = tools.call("recommend_tvs", {"use_cases": ["gaming"], "required_features": ["hdmi_2_1", "hz_120"],
                                           "preferred_features": ["allm", "vrr"]}, "t1", conversation(PS5))
    assert guarded["status"] == "ok" and codes(guarded) == codes(plain)                 # ranking untouched
    for before, after in zip(plain["products"], guarded["products"]):
        assert after["features"]["hdmi_2_1"] == "not_listed"
        assert {f: after["features"][f] for f in before["features"]} == before["features"]
        assert (after["current_price_rub"], after["available"]) == (before["current_price_rub"], before["available"])
    assert {c["value"] for c in guarded["request"]["not_applied"]["constraints"]} == {"hdmi_2_1", "hz_120"}
    assert guarded["request"]["features_checked"] == ["hz_120", "hdmi_2_1"]
    # a strong result becomes partial; this fixture's gaming result is weak already (top band larger than the shortlist)
    assert guarded["confidence"] == ("partial" if plain["confidence"] == "strong" else plain["confidence"]) == "weak"
    oled65 = {"panel_technology": ["OLED"], "screen_size_inches": 65, "max_price": 200000, "use_cases": ["gaming"]}
    strong = tools.call("recommend_tvs", oled65)
    partial = tools.call("recommend_tvs", {**oled65, "required_features": ["hdmi_2_1"]}, "t9",
                         conversation("OLED 65 дюймов до 200 тысяч для PS5"))
    assert (strong["confidence"], partial["confidence"]) == ("strong", "partial") and codes(strong) == codes(partial)
    assert any("not listed in the catalog" in n for n in partial["confidence_notes"])
    assert any(g["kind"] == "attribute_not_listed_for_product" and g["attributes"] == ["hdmi_2_1"] for g in guarded["gaps"])
    assert guarded["feature_summary"]["counts"]["hdmi_2_1"] == {"not_listed": len(guarded["products"])}
    assert plain["feature_summary"] == guarded["feature_summary"]                       # the summary needs no guard context


def test_list_summary_matches_the_products_it_summarizes(env):
    tools, repo = env
    r = tools.call("search_tvs", {"panel_technology": ["OLED"]})
    counts = r["feature_summary"]["counts"]
    assert set(counts) == set(SUMMARY_FEATURE_IDS) and r["feature_summary"]["products"] == len(r["products"]) == 11
    rows = {p.model_code: p for p in repo.get_products_by_codes(codes(r))}
    specs = repo.get_specs([p.id for p in rows.values()])
    for f in SUMMARY_FEATURE_IDS:
        states = [evaluate_features([rows[c]], specs, [f])[rows[c].id][f].state.value for c in codes(r)]
        assert counts[f] == {s: states.count(s) for s in ("yes", "no", "not_listed") if states.count(s)}, f
    assert "features" not in r["products"][0]                                           # nothing asked: nothing per product


def test_overview_lookup_reports_a_feature_the_user_asked_about(env):
    tools, _ = env
    r = tools.call("get_tv", {"model": "QE65S95HAUXPY"}, "t2", conversation(PS5, "А HDMI 2.1 у него точно есть?"))
    assert r["products"][0]["features"] == {"hdmi_2_1": "not_listed"} and "feature_summary" not in r
    listed = tools.call("get_tv", {"model": "MNA114MS1CCXRU"}, "t3", conversation("а у него HDMI 2.1?"))
    assert listed["products"][0]["features"] == {"hdmi_2_1": "yes"}                     # evidenced yes stays yes


# ---- MVP-3 ------------------------------------------------------------------------------------------------

def test_feature_count_is_exact_and_is_not_the_availability_count(env):
    """4F.2 PA-15 turn 3 with the fixture's numbers: 30 available products, 21 of them list Dolby Atmos."""
    tools, _ = env
    r = tools.call("get_catalog_stats", {"stat": "count", "availability": "available", "attributes": ["dolby_atmos"]})
    assert r["status"] == "ok" and r["counts"] == {"available": 30}
    atmos = r["attribute_counts"]["dolby_atmos"]
    assert atmos == {"available": {"yes": 21, "no": 0, "not_listed": 9}} == {"available": expected_states("dolby_atmos", True)}
    assert atmos["available"]["yes"] != r["counts"]["available"]                         # the count that was confused
    assert sum(atmos["available"].values()) == r["counts"]["available"]
    assert r["counted"] == "products matching: is_available = True"
    assert "not counts of products with any feature" in r["scope_note"]


def test_a_plain_count_carries_no_feature_information(env):
    tools, _ = env
    r = tools.call("get_catalog_stats", {"stat": "count", "availability": "available"})
    assert r["counts"] == {"available": 30} and "attribute_counts" not in r
    assert r["counted"] == "products matching: is_available = True" and r["scope_note"]
    whole = tools.call("get_catalog_stats", {"stat": "count"})
    assert whole["counts"] == {"total": 31, "available": 30, "unavailable": 1} and whole["counted"] == "all products in the catalog"


@pytest.mark.parametrize("feature", sorted(FEATURES))
def test_every_registry_feature_is_counted_exactly_in_every_bucket(env, feature):
    tools, _ = env
    r = tools.call("get_catalog_stats", {"stat": "count", "attributes": [feature]})
    got = r["attribute_counts"][feature]
    assert got == {"total": expected_states(feature), "available": expected_states(feature, True),
                   "unavailable": expected_states(feature, False)}
    assert {k: sum(v.values()) for k, v in got.items()} == r["counts"]


def test_feature_count_is_scoped_to_the_filters_that_were_passed(env):
    tools, repo = env
    r = tools.call("get_catalog_stats", {"stat": "count", "panel_technology": ["OLED"], "attributes": ["hz_120", "vrr"]})
    assert r["counts"] == {"total": 11, "available": 11, "unavailable": 0}
    assert r["counted"] == "products matching: panel_technology in ['OLED']"
    assert r["attribute_counts"]["hz_120"]["total"] == {"yes": 11, "no": 0, "not_listed": 0}
    assert r["attribute_counts"]["vrr"]["unavailable"] == {"yes": 0, "no": 0, "not_listed": 0}
    whole = tools.call("get_catalog_stats", {"stat": "count", "attributes": ["hz_120"]})
    assert whole["attribute_counts"]["hz_120"]["total"] == {"yes": 22, "no": 9, "not_listed": 0}   # the subset is not the catalog
    assert repo.count(Filters(panel_technology=("OLED",)))[0][1] == 11


def test_series_scope_counts_every_member_including_unavailable_ones(env):
    tools, _ = env
    r = tools.call("get_catalog_stats", {"stat": "count", "model": "S95H", "attributes": ["allm"]})
    assert r["counts"] == {"total": 4, "available": 4, "unavailable": 0}
    assert r["counted"] == "the 4 catalog product(s) of family 'S95H'"
    assert r["request"]["model_resolution"] == [{"input": "S95H", "status": "family", "sizes": [55, 65, 77, 83]}]
    assert sum(r["attribute_counts"]["allm"]["total"].values()) == 4
    one = tools.call("get_catalog_stats", {"stat": "count", "model": "QN1E"})          # the fixture's unavailable product
    assert one["counts"] == {"total": 1, "available": 0, "unavailable": 1}
    assert tools.call("search_tvs", {"category": ["Neo QLED"], "screen_size_inches": 55})["status"] == "ok"   # the list hides it
    narrowed = tools.call("get_catalog_stats", {"stat": "count", "model": "S95H", "max_price": 250000})
    assert narrowed["counts"]["total"] == 1 and narrowed["counted"].endswith("matching: effective_price <= 250000")


def test_unknown_series_counts_nothing(env):
    tools, _ = env
    r = tools.call("get_catalog_stats", {"stat": "count", "model": "S80C", "attributes": ["vrr"]})
    assert r["status"] == "not_found" and "counts" not in r and "attribute_counts" not in r
    assert r["gaps"] == [{"kind": "model_not_found", "detail": "'S80C' is not in the catalog"}]
    typo = tools.call("get_catalog_stats", {"stat": "count", "model": "QE55S90HAEXPY"})
    assert typo["status"] == "not_found" and typo["request"]["model_resolution"][0]["suggestions"]


def test_a_no_for_120_hz_comes_with_the_refresh_rates_the_products_have(env):
    """Phase 4F.3 targeted run 1, PA-15 turn 2: 'hz_120: no for all' was read as 'all are 60 Hz' although two of
    the products are 50 Hz. The count now lists the values, so no value has to be inferred from a 'no'."""
    tools, repo = env
    catalog = repo.search(Filters(), limit=50).rows
    band = next(p.effective_price for p in catalog if p.refresh_rate_hz == 50)          # the fixture's one 50 Hz product
    r = tools.call("get_catalog_stats", {"stat": "count", "max_price": band, "attributes": ["hz_120"]})
    rows = [p for p in catalog if p.effective_price <= band]
    assert r["attribute_counts"]["hz_120"]["total"] == {"yes": 0, "no": len(rows), "not_listed": 0}
    rates = [p.refresh_rate_hz for p in rows]
    assert r["attribute_values"]["hz_120"]["total"] == {str(v): rates.count(v) for v in sorted(set(rates))}
    assert set(r["attribute_values"]["hz_120"]["total"]) == {"50", "60"}                # "no" for all, and two rates
    assert sum(r["attribute_values"]["hz_120"]["total"].values()) == r["counts"]["total"]
    assert list(r).index("attribute_counts") + 1 == list(r).index("attribute_values")
    sound = tools.call("get_catalog_stats", {"stat": "count", "panel_technology": ["OLED"], "attributes": ["sound_power_w", "vrr"]})
    assert set(sound["attribute_values"]) == {"sound_power_w"}                           # vrr has no value: counts only
    assert sum(sound["attribute_values"]["sound_power_w"]["total"].values()) == sound["counts"]["total"] == 11
    assert "unavailable" not in sound["attribute_values"]["sound_power_w"]               # no product in that bucket
    depth = tools.call("get_catalog_stats", {"stat": "count", "attributes": ["depth_cm"]})["attribute_values"]["depth_cm"]["total"]
    assert set(depth) == {"min", "max", "distinct_values"} and depth["min"] < depth["max"] and depth["distinct_values"] > 12
    assert "attribute_values" not in tools.call("get_catalog_stats", {"stat": "count", "attributes": ["dolby_atmos"]})


def test_refresh_rates_that_exist_in_a_price_band(env):
    """PA-15 turn 2 ('are they all 60 Hz?'): the distribution, with unavailable products included."""
    tools, repo = env
    r = tools.call("get_catalog_stats", {"stat": "count", "max_price": 50000, "group_by": "refresh_rate_hz"})
    assert r["group_by"] == "refresh_rate_hz" and sum(g["count"] for g in r["groups"]) == r["counts"]["total"]
    assert {g["value"] for g in r["groups"]} == {p.refresh_rate_hz for p in repo.search(Filters(effective_price=None), limit=50).rows
                                                 if p.effective_price <= 50000}
    assert all(set(g) == {"value", "count", "available"} for g in r["groups"])


def test_existing_stats_calls_are_unchanged_apart_from_the_scope_fields(env):
    tools, _ = env
    r = tools.call("get_catalog_stats", {"stat": "count", "panel_technology": ["Neo QLED"]})
    assert r["counts"] == {"total": 6, "available": 5, "unavailable": 1} and r["status"] == "ok"
    assert set(r) == {"contract", "tool", "status", "confidence", "stat", "request", "counted", "counts", "scope_note",
                      "data_notice"}
    cheapest = tools.call("get_catalog_stats", {"stat": "cheapest"})
    assert codes(cheapest) == ["UE32H5000FUXRU"] and "feature_summary" not in cheapest and "counted" not in cheapest
