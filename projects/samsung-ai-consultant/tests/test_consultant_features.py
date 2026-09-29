"""Feature registry v1: tri-state semantics against real (fixture) catalog values."""

import pytest

from consultant.features import (
    FEATURES, REGISTRY_SPEC_NAMES, USE_CASES, evaluate_features, parse_dimensions_cm, spec_names_for,
)
from consultant.schemas import FeatureState, ProductKind, ProductRow, SpecRow

from .consultant_fixtures import product_rows

YES, NO, NL = FeatureState.YES, FeatureState.NO, FeatureState.NOT_LISTED


@pytest.fixture(scope="module")
def evaluated():
    rows, specs, by_code = product_rows()
    return evaluate_features(rows, specs, list(FEATURES)), by_code


def state(evaluated, code, fid):
    res, by_code = evaluated
    return res[by_code[code].id][fid]


def test_registry_is_closed_and_versioned():
    with pytest.raises(KeyError):
        evaluate_features([], {}, ["brightness_nits"])
    assert "hdmi_2_1" in FEATURES and "brightness_nits" not in FEATURES
    assert spec_names_for(["vrr"]) == ("Другие технологии оптимизации изображения",)
    assert set(spec_names_for(list(FEATURES))) == set(REGISTRY_SPEC_NAMES)


@pytest.mark.parametrize("code,fid,expected", [
    ("MNA114MS1CCXRU", "hdmi_2_1", YES),          # the only product with 'Версия HDMI: 2.1'
    ("QE65S95HAUXPY", "hdmi_2_1", NL),            # no HDMI version spec -> not_listed, never 'no'
    ("QE65S95HAUXPY", "vrr", YES),
    ("UE32H5000FUXRU", "vrr", NL),
    ("QE65S95HAUXPY", "hz_120", YES),
    ("UE32H5000FUXRU", "hz_120", NO),             # typed column 60 Hz: an explicit negative
    ("QE65S95HAUXPY", "freesync_premium", YES),
    ("QE65S95HAUXPY", "anti_glare", YES),
    ("MRE115MR95FXRU", "anti_glare", YES),        # value 'Anti Reflection'
    ("UE32H5000FUXRU", "anti_glare", NL),
    ("QE65S95HAUXPY", "filmmaker_mode", YES),
    ("QE65S95HAUXPY", "dolby_atmos", YES),
    ("QE32LS03CBUXRU", "vesa", NO),               # 'Стандарт VESA: Нет'
    ("QE65S95HAUXPY", "vesa", YES),
])
def test_boolean_features_on_real_values(evaluated, code, fid, expected):
    assert state(evaluated, code, fid).state is expected


def test_allm_states_present_in_fixture(evaluated):
    res, by_code = evaluated
    states = {res[p.id]["allm"].state for p in by_code.values()}
    assert states == {YES, NL}                     # catalog only ever says 'Да'; absence is not 'no'


def test_freesync_tiers(evaluated):
    res, by_code = evaluated
    pro = {res[p.id]["freesync_premium_pro"].state for p in by_code.values()}
    assert pro == {YES, NO, NL}                    # 'FreeSync Premium' is an explicit lower tier -> no


def test_evidence_identifies_source(evaluated):
    r = state(evaluated, "MNA114MS1CCXRU", "hdmi_2_1")
    assert r.evidence[0].origin == "spec" and r.evidence[0].name == "Версия HDMI" and r.evidence[0].value == "2.1"
    hz = state(evaluated, "UE32H5000FUXRU", "hz_120")
    assert hz.evidence[0].origin == "column" and hz.evidence[0].name == "refresh_rate_hz"


def _one(specs: dict, **cols):
    base = dict(id=1, source="s", external_id="1", model_code="X", name="Телевизор X", category=None,
                series=None, product_kind=ProductKind.TV, year=2026, screen_size_inches=65.0, resolution=None,
                panel_technology=None, refresh_rate_hz=None, price=None, sale_price=None, effective_price=None,
                currency="RUB", is_available=True, product_url=None)
    base.update(cols)
    p = ProductRow(**base)
    rows = [SpecRow(1, "g", k, k, v) for k, v in specs.items()]
    return p, {1: rows}


@pytest.mark.parametrize("value,expected", [("2.1", YES), ("2.0", NO), ("HDMI 2.1a", YES), ("???", NL)])
def test_hdmi_2_1_tri_state(value, expected):
    p, specs = _one({"Версия HDMI": value})
    assert evaluate_features([p], specs, ["hdmi_2_1"])[1]["hdmi_2_1"].state is expected


def test_explicit_negative_vs_absent():
    p, specs = _one({"Антибликовое покрытие": "Нет"})
    assert evaluate_features([p], specs, ["anti_glare"])[1]["anti_glare"].state is NO
    p, specs = _one({})
    assert evaluate_features([p], specs, ["anti_glare"])[1]["anti_glare"].state is NL


def test_list_value_without_item_is_not_listed_not_no():
    p, specs = _one({"Поддержка форматов звука": "Dolby Digital"})
    assert evaluate_features([p], specs, ["dolby_atmos"])[1]["dolby_atmos"].state is NL


def test_sound_power(evaluated):
    assert state(evaluated, "QE65S95HAUXPY", "sound_power_w").value == 70.0
    p, specs = _one({"Мощность звука, Вт": "2 x 10"})
    r = evaluate_features([p], specs, ["sound_power_w"])[1]["sound_power_w"]
    assert r.state is NL and r.data_quality == "unparseable_number"


# The exact non-canonical dimension strings found in production (inventory / Phase 3D.4).
@pytest.mark.parametrize("raw,size,dims,reason", [
    ("150.95 x 89.49 x 2.64", 65, (150.95, 89.49, 2.64), None),
    ("257.4 х 147.8 х 3.57", 115, (257.4, 147.8, 3.57), None),        # Cyrillic 'х' -- tolerated
    ("96.5\tx 56.3 x 7.5", 43, (96.5, 56.3, 7.5), None),               # tab -- tolerated
    ("123.79 x 70.8.8 x 2.49", 55, None, "malformed_component"),       # never guess the depth
    ("122.57 x 70.5.6 x 3.39", 55, None, "malformed_component"),
    ("2229.8 x 1273.4 x 57.4", 100, None, "unit_scale_mismatch"),      # evidently mm, not cm
    ("144.29 x 82.92 x 52", 65, None, "implausible_depth"),            # siblings are 5.2 cm
    ("150 x 90", 65, None, "unexpected_component_count"),
])
def test_dimension_parser(raw, size, dims, reason):
    assert parse_dimensions_cm(raw, size) == (dims, reason)


@pytest.mark.parametrize("code,value,quality", [
    ("QE65S95HAUXPY", 2.64, None),
    ("MRE115MR95FXRU", 3.57, None),
    ("QE55LS03HAUXPY", None, "malformed_component"),
    ("QE55S85HAEXPY", None, "malformed_component"),
    ("MRE100R85HUXPY", None, "unit_scale_mismatch"),
    ("QE65QN80HAUXPY", None, "implausible_depth"),
])
def test_depth_feature_data_quality(evaluated, code, value, quality):
    r = state(evaluated, code, "depth_cm")
    assert (r.value, r.data_quality) == (value, quality)
    assert r.state is (YES if value is not None else NL)


def test_use_case_profiles():
    assert set(USE_CASES) == {"gaming", "movies", "sound", "bright_room", "thin_wall", "compact"}
    assert "hdmi_2_1" not in USE_CASES["gaming"].preferred       # 1/75 coverage: not a ranking signal
    assert USE_CASES["bright_room"].gaps[0][0] == "brightness_not_in_catalog"
    assert USE_CASES["compact"].preferred == ()                   # size is a soft signal, not a filter
    for uc in USE_CASES.values():
        assert all(f in FEATURES for f in uc.preferred)
