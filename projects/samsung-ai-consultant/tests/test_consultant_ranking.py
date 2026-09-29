"""Ranking v1 and shortlist composition are separate, and composition never redefines fit."""

from dataclasses import replace

import pytest

from consultant.features import USE_CASES, evaluate_features
from consultant.ranking import (
    POLICY_PANEL_DIVERSITY, POLICY_PRICE_TIERS, POLICY_TOP_RANKED, choose_policy, compose_shortlist,
    rank_candidates,
)
from consultant.schemas import FeatureResult, FeatureState, ProductKind, ProductRow

from .consultant_fixtures import product_rows

YES, NO, NL = FeatureState.YES, FeatureState.NO, FeatureState.NOT_LISTED


def _p(i, price, panel="OLED"):
    return ProductRow(i, "galaxystore", f"{i:03d}", f"M{i:03d}", f"Телевизор {i}", panel, None, ProductKind.TV,
                      2026, 55.0, "3840x2160", panel, 120, price, None, price, "RUB", True, None)


def _res(**states):
    return {f: FeatureResult(f, s) for f, s in states.items()}


def test_required_gate_separates_unknown_from_rejected():
    ps = [_p(1, 100), _p(2, 90), _p(3, 80)]
    fr = {1: _res(vrr=YES), 2: _res(vrr=NL), 3: _res(vrr=NO)}
    r = rank_candidates(ps, fr, required=["vrr"])
    assert [c.product.id for c in r.qualified] == [1]
    assert [c.product.id for c in r.unknown] == [2]       # not_listed is kept, never treated as 'no'
    assert [p.id for p in r.rejected] == [3]


def test_preferred_count_then_price_then_identity():
    ps = [_p(1, 300), _p(2, 100), _p(3, 200), _p(4, 100)]
    fr = {1: _res(a=YES, b=YES), 2: _res(a=YES, b=NL), 3: _res(a=YES, b=NO), 4: _res(a=YES, b=NL)}
    r = rank_candidates(ps, fr, preferred=["a", "b"])
    assert [c.product.id for c in r.qualified] == [1, 2, 4, 3]
    top = r.qualified[0].explain()
    assert top["preferred_matched"] == 2 and top["preferred_total"] == 2 and top["effective_price"] == 300


def test_numeric_signal_breaks_ties_before_price():
    rows, specs, by_code = product_rows()
    ps = [by_code[c] for c in ("UE32H5000FUXRU", "QE65S95HAUXPY", "QE55S95HAUXPY", "QE75QN80HAUXPY")]
    prof = USE_CASES["sound"]
    fr = evaluate_features(ps, specs, ["dolby_atmos", "sound_power_w"])
    r = rank_candidates(ps, fr, preferred=prof.preferred, numeric=prof.numeric)
    order = [c.product.model_code for c in r.qualified]
    assert order[:2] == ["QE55S95HAUXPY", "QE65S95HAUXPY"]    # 70 W both; cheaper first
    assert r.qualified[0].numeric_signals == (("sound_power_w", 70.0),)


def test_missing_numeric_value_sorts_last():
    ps = [_p(1, 100), _p(2, 500)]
    fr = {1: {"depth_cm": FeatureResult("depth_cm", NL, data_quality="malformed_component")},
          2: {"depth_cm": FeatureResult("depth_cm", YES, value=2.5)}}
    prof = USE_CASES["thin_wall"]
    r = rank_candidates(ps, fr, numeric=prof.numeric)
    assert [c.product.id for c in r.qualified] == [2, 1]


def test_choose_policy():
    assert choose_policy(has_budget=True, has_preferences=True) == POLICY_TOP_RANKED
    assert choose_policy(has_budget=False, has_preferences=True) == POLICY_PRICE_TIERS
    assert choose_policy(has_budget=False, has_preferences=False) == POLICY_PANEL_DIVERSITY


def _ranking_with_bands():
    # 12 products tied at the top fit (2 prefs), priced 10..120; 3 lower-fit cheap products.
    top = [_p(i, 10 * i) for i in range(1, 13)]
    low = [_p(100 + i, 1) for i in range(3)]
    fr = {p.id: _res(a=YES, b=YES) for p in top} | {p.id: _res(a=YES, b=NO) for p in low}
    return rank_candidates(top + low, fr, preferred=["a", "b"])


def test_price_tiers_pick_across_the_band_without_reordering():
    r = _ranking_with_bands()
    s = compose_shortlist(r, POLICY_PRICE_TIERS)
    assert s.policy == POLICY_PRICE_TIERS and len(s.items) == 8
    assert set(s.tiers.values()) == {"low", "mid", "high"}
    ranks = [c.rank for c in s.items]
    assert ranks == sorted(ranks)                                   # exposure keeps ranking order
    assert all(c.preferred_matched == 2 for c in s.items)          # no lower-fit product admitted
    assert r.qualified[0].product.id == 1                           # ranking itself untouched


def test_composition_never_lets_lower_fit_displace_higher_fit():
    r = _ranking_with_bands()
    for policy in (POLICY_TOP_RANKED, POLICY_PRICE_TIERS, POLICY_PANEL_DIVERSITY):
        s = compose_shortlist(r, policy)
        worst_in = min(c.preferred_matched for c in s.items)
        excluded = [c for c in r.qualified if c not in s.items]
        assert all(c.preferred_matched <= worst_in for c in excluded)


def test_diversity_not_applied_when_top_band_fits_in_shortlist():
    ps = [_p(1, 300), _p(2, 200), _p(3, 100)] + [_p(10 + i, 5) for i in range(9)]
    fr = {1: _res(a=YES), 2: _res(a=YES), 3: _res(a=YES)} | {10 + i: _res(a=NO) for i in range(9)}
    r = rank_candidates(ps, fr, preferred=["a"])
    s = compose_shortlist(r, POLICY_PRICE_TIERS)
    assert s.policy == POLICY_TOP_RANKED and s.notes
    assert [c.product.id for c in s.items[:3]] == [3, 2, 1]


def test_numeric_signal_is_part_of_fit_band():
    # 10 products with equal preferred count but different sound power: tiers must not pull a
    # 20 W product in ahead of 70 W ones.
    ps = [_p(i, 10 * i) for i in range(1, 11)]
    fr = {p.id: {"dolby_atmos": FeatureResult("dolby_atmos", YES),
                 "sound_power_w": FeatureResult("sound_power_w", YES, value=70.0 if p.id <= 3 else 20.0)}
          for p in ps}
    prof = USE_CASES["sound"]
    r = rank_candidates(ps, fr, preferred=prof.preferred, numeric=prof.numeric)
    s = compose_shortlist(r, POLICY_PRICE_TIERS)
    assert s.policy == POLICY_TOP_RANKED
    assert [c.product.id for c in s.items[:3]] == [1, 2, 3]


def test_panel_diversity_round_robin():
    ps = [_p(i, i, "OLED") for i in range(1, 7)] + [_p(10 + i, 10 + i, "Neo QLED") for i in range(6)]
    fr = {p.id: {} for p in ps}
    r = rank_candidates(ps, fr)
    s = compose_shortlist(r, POLICY_PANEL_DIVERSITY)
    panels = [c.product.panel_technology for c in s.items]
    assert panels.count("OLED") == 4 and panels.count("Neo QLED") == 4


def test_policy_can_be_disabled_without_touching_ranking():
    r = _ranking_with_bands()
    assert [c.rank for c in compose_shortlist(r, POLICY_TOP_RANKED).items] == list(range(1, 9))
    with pytest.raises(ValueError):
        compose_shortlist(r, "tiers-v99")


def test_shortlist_cap():
    r = _ranking_with_bands()
    assert len(compose_shortlist(r, POLICY_TOP_RANKED, max_items=3).items) == 3
    assert replace(r, qualified=()).qualified == ()
