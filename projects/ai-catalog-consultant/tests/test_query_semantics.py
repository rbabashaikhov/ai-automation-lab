"""Phase 4E.1 query-semantics spike: contract, parser, safety traps, gold dataset, isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from consultant import query_semantics as qs
from consultant.query_semantics import (
    Preference, QuerySemantics, SemanticIntent, SemanticValidationError, parse_query_semantics,
)
from evaluation import semantic_eval

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def vocab():
    return semantic_eval.fixture_vocab()


def parse(text, vocab=None) -> QuerySemantics:
    p = parse_query_semantics(text, vocab)
    assert p.ok, p.error
    return p.semantics


# ---- contract -------------------------------------------------------------------------------

def test_valid_minimal_semantics():
    s = QuerySemantics.from_dict({"filters": {}, "preferences": []})
    assert s.intent is None and s.filters.to_dict() == {} and s.preferences == () and s.notes == ()
    assert QuerySemantics.from_dict({"intent": None, "filters": {}, "preferences": []}).intent is None


def test_round_trip():
    raw = {"intent": "recommendation", "filters": {"panel_technology": ["OLED"], "max_price": 150000},
           "preferences": ["gaming", "audio"], "notes": ["vague_size"]}
    s = QuerySemantics.from_dict(raw)
    assert s.intent is SemanticIntent.RECOMMENDATION and s.preferences == (Preference.GAMING, Preference.AUDIO)
    assert QuerySemantics.from_dict(s.to_dict()) == s


@pytest.mark.parametrize("raw, message", [
    ({"intent": "recommend", "filters": {}, "preferences": []}, "intent"),
    ({"intent": "lookup", "filters": {}, "preferences": []}, "intent"),
    ({"filters": {}, "preferences": ["cheap"]}, "preferences"),
    ({"filters": {}, "preferences": ["gaming", "gaming"]}, "duplicate"),
    ({"filters": {}, "preferences": [], "weights": {}}, "unknown field"),
    ({"filters": {"vrr": True}, "preferences": []}, "unknown field"),
    ({"filters": {"hdmi_2_1": "required"}, "preferences": []}, "unknown field"),
    ({"filters": {"brightness_nits": 500}, "preferences": []}, "unknown field"),
    ({"filters": {"screen_size_inches": "65"}, "preferences": []}, "expected number"),
    ({"filters": {"screen_size_inches": True}, "preferences": []}, "expected number"),
    ({"filters": {"screen_size_inches": 500}, "preferences": []}, "<= 130"),
    ({"filters": {"max_price": -1}, "preferences": []}, ">= 0"),
    ({"filters": {"panel_technology": ["Plasma"]}, "preferences": []}, "panel_technology"),
    ({"filters": {"resolution": ["16K"]}, "preferences": []}, "resolution"),
    ({"filters": {"min_refresh_rate_hz": 120.5}, "preferences": []}, "expected integer"),
    ({"filters": {"availability": "yes"}, "preferences": []}, "availability"),
    ({"filters": {"models": ["S95H; DROP TABLE"]}, "preferences": []}, "malformed"),
    ({"filters": {"screen_size_inches": 65, "min_screen_size_inches": 55}, "preferences": []}, "not both"),
    ({"filters": {"min_price": 200000, "max_price": 100000}, "preferences": []}, "min_price > max_price"),
    ({"preferences": []}, "filters: required"),
    ({"filters": {}, "preferences": [], "notes": ["Free text!"]}, "malformed"),
    ("recommendation", "expected object"),
])
def test_invalid_semantics_rejected(raw, message):
    with pytest.raises(SemanticValidationError, match=message):
        QuerySemantics.from_dict(raw)


def test_filter_schema_is_the_tool_contract():
    """Semantics filters are a subset of the published search_tvs arguments (plus compare_tvs models)."""
    tool = qs.TOOL_SCHEMAS["search_tvs"]["inputSchema"]["properties"]
    props = qs.SEMANTICS_SCHEMA["properties"]["filters"]["properties"]
    assert set(props) - {"models"} <= set(tool)
    assert all(props[k] is tool[k] for k in props if k != "models")
    assert not {"required_features", "preferred_features", "use_cases"} & set(props)


# ---- parser ---------------------------------------------------------------------------------

def test_multiple_preferences():
    s = parse("Для PS5 и кино, чтобы звук был хороший и в светлой комнате не бликовал")
    assert set(s.preferences) == {Preference.GAMING, Preference.MOVIES, Preference.AUDIO, Preference.BRIGHT_ROOM}
    assert s.filters.to_dict() == {}


def test_mixed_query_gold_case():
    s = parse("Хочу 65-дюймовый OLED до 150 тысяч для PS5 и кино, чтобы звук был хороший.")
    assert s.intent is SemanticIntent.RECOMMENDATION
    assert s.filters.to_dict() == {"panel_technology": ["OLED"], "screen_size_inches": 65.0, "max_price": 150000.0}
    assert set(s.preferences) == {Preference.GAMING, Preference.MOVIES, Preference.AUDIO}


def test_comparison_intent(vocab):
    s = parse("Что лучше для PS5: S85H или S90H?", vocab)
    assert s.intent is SemanticIntent.COMPARISON
    assert s.filters.models == ("S85H", "S90H") and s.preferences == (Preference.GAMING,)
    assert parse("S90H или S95H?", vocab).intent is SemanticIntent.COMPARISON


def test_product_question_intent(vocab):
    s = parse("Какая частота обновления у S95H?", vocab)
    assert s.intent is SemanticIntent.PRODUCT_QUESTION and s.filters.to_dict() == {"models": ["S95H"]}
    s = parse("Подходит ли QE65S95HAUXPY для игр?", vocab)
    assert s.intent is SemanticIntent.PRODUCT_QUESTION and s.preferences == (Preference.GAMING,)


@pytest.mark.parametrize("text, filters", [
    ("Какие OLED модели есть?", {"panel_technology": ["OLED"]}),
    ("Сколько моделей дешевле 70 тысяч?", {"max_price": 70000.0}),
    ("Какой самый дешёвый телевизор?", {}),
])
def test_catalog_question_is_not_a_recommendation(text, filters):
    s = parse(text)
    assert s.intent is SemanticIntent.CATALOG_QUESTION and s.filters.to_dict() == filters


@pytest.mark.parametrize("text, filters", [
    ("Посоветуй телевизор со 120 Гц до 150 тысяч", {"min_refresh_rate_hz": 120, "max_price": 150000.0}),
    ("не меньше 55 дюймов", {"min_screen_size_inches": 55.0}),
    ("от 75 дюймов", {"min_screen_size_inches": 75.0}),
    ("до 50 дюймов", {"max_screen_size_inches": 50.0}),
    ("не меньше 100 тысяч", {"min_price": 100000.0}),        # the 4B extractor reads this as a maximum
    ("не больше 100 тысяч", {"max_price": 100000.0}),        # ... and this as a minimum
    ("не дешевле 100 тысяч", {"min_price": 100000.0}),
    ("Нужен 4K телевизор на 65 дюймов", {"resolution": ["4K"], "screen_size_inches": 65.0}),
    ("OLED с ценой без скидки не больше 300 тысяч", {"panel_technology": ["OLED"], "max_price": 300000.0,
                                                     "price_basis": "list"}),
    ("Какие модели сейчас нет в наличии?", {"availability": "unavailable"}),
])
def test_explicit_filters(text, filters):
    assert parse(text).filters.to_dict() == filters


@pytest.mark.parametrize("text, note", [
    ("Посоветуй телевизор на 55 дюймов, только не OLED.", "negated_panel_technology:oled"),
    ("Нужен телевизор для кино, OLED не принципиален.", "optional_panel_technology:oled"),
    ("Хочу телевизор без OLED", "negated_panel_technology:oled"),
])
def test_negated_or_optional_panel_never_becomes_a_filter(text, note):
    s = parse(text)
    assert "panel_technology" not in s.filters.to_dict() and note in s.notes


def test_optional_panel_does_not_swallow_the_previous_clause():
    assert parse("Нужен телевизор для кино, OLED не принципиален.").preferences == (Preference.MOVIES,)


@pytest.mark.parametrize("text", ["Хочется нормальный звук без отдельной акустики.",
                                  "Не хочу покупать отдельный саундбар"])
def test_no_external_speaker_is_an_audio_wish_not_an_exclusion(text):
    s = parse(text)
    assert s.preferences == (Preference.AUDIO,) and s.filters.to_dict() == {}


def test_size_alternatives_are_not_collapsed_to_one_size():
    s = parse("Нужен телевизор 55 или 65 дюймов")
    assert s.filters.to_dict() == {} and "size_alternatives" in s.notes


def test_general_question_has_no_catalog_intent_and_no_filters():
    s = parse("Объясни простыми словами, что такое OLED.")
    assert s.intent is None and s.filters.to_dict() == {} and s.preferences == ()


def test_named_feature_stays_unstructured():
    s = parse("Нужен телевизор для PS5 со 120 Гц и HDMI 2.1, до 120 тысяч.")
    assert s.filters.to_dict() == {"min_refresh_rate_hz": 120, "max_price": 120000.0}
    assert "feature_mention:hdmi_2_1" in s.notes


# ---- mandatory false-constraint traps -------------------------------------------------------

HARD_FEATURE_WORDS = ("refresh", "vrr", "hdmi", "bright", "nit", "sound", "audio", "watt", "power")


def _assert_no_hard_constraint(s: QuerySemantics):
    """No filter at all, and the shadow tool call never carries required features."""
    assert s.filters.to_dict() == {}
    call = semantic_eval.shadow_tool_call(s)
    assert "required_features" not in call["args"] and "preferred_features" not in call["args"]
    assert not any(w in k for k in call["args"] for w in HARD_FEATURE_WORDS)
    check = semantic_eval.boundary_check(call, semantic_eval.fixture_vocab())
    assert check["valid"] is True and check["resolved"]["required"] == []


@pytest.mark.parametrize("text", ["для PS5", "Посоветуй телевизор для PS5.", "Нужен телек под плойку.",
                                  "телевизор для Xbox Series X", "хочу играть на приставке"])
def test_ps5_is_gaming_not_120hz_vrr_or_hdmi_2_1(text):
    s = parse(text)
    assert s.preferences == (Preference.GAMING,)
    assert s.filters.min_refresh_rate_hz is None
    _assert_no_hard_constraint(s)


@pytest.mark.parametrize("text", ["недорогой", "Посоветуй недорогой телевизор.", "что-нибудь подешевле",
                                  "бюджетный телевизор"])
def test_cheap_never_invents_a_price(text):
    s = parse(text)
    assert s.filters.min_price is None and s.filters.max_price is None and "vague_price" in s.notes
    _assert_no_hard_constraint(s)


@pytest.mark.parametrize("text", ["большой телевизор", "Хочу большой телевизор в гостиную.", "экран побольше"])
def test_large_never_invents_a_diagonal(text):
    s = parse(text)
    assert "vague_size" in s.notes
    _assert_no_hard_constraint(s)


@pytest.mark.parametrize("text", ["мощный звук", "Хочу телевизор с мощным звуком.", "чтобы было громко"])
def test_powerful_sound_never_invents_wattage(text):
    s = parse(text)
    assert s.preferences == (Preference.AUDIO,) and "vague_audio_power" in s.notes
    _assert_no_hard_constraint(s)


@pytest.mark.parametrize("text, pref", [("для светлой комнаты", Preference.BRIGHT_ROOM),
                                        ("Чтобы днём картинка не была блеклой.", Preference.BRIGHT_ROOM),
                                        ("яркий экран", Preference.PICTURE_QUALITY)])
def test_bright_room_or_bright_screen_never_invents_brightness(text, pref):
    s = parse(text)
    assert pref in s.preferences
    _assert_no_hard_constraint(s)


def test_asking_about_an_attribute_is_not_a_constraint(vocab):
    s = parse("Какая частота обновления у S95H?", vocab)
    assert s.filters.min_refresh_rate_hz is None


# ---- failure contract -----------------------------------------------------------------------

@pytest.mark.parametrize("query, error", [("", "empty_query"), ("   ", "empty_query"), (None, "empty_query"),
                                          (42, "empty_query"), ("x" * 1001, "query_too_long")])
def test_parser_failure_is_unavailable_not_empty(query, error):
    p = parse_query_semantics(query)
    assert p.status == "unavailable" and not p.ok and p.semantics is None and p.error == error


def test_internal_error_is_unavailable_and_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internals")
    monkeypatch.setattr(qs, "extract", boom)
    p = parse_query_semantics("OLED до 100 тысяч")
    assert p.status == "unavailable" and p.semantics is None and p.error == "RuntimeError"


def test_invalid_parser_output_is_unavailable(monkeypatch):
    """The parser's own output passes the same validator an LLM backend would; a bad value is caught."""
    monkeypatch.setattr(qs, "_filters", lambda *a: {"max_price": -5})
    p = parse_query_semantics("до 100 тысяч")
    assert p.status == "unavailable" and p.error == "SemanticValidationError"


def test_ok_with_no_filters_is_distinct_from_unavailable():
    p = parse_query_semantics("Посоветуй телевизор.")
    assert p.ok and p.semantics.filters.to_dict() == {}


# ---- gold dataset and evaluation ------------------------------------------------------------

def test_gold_fixtures_validate_and_ids_are_unique():
    cases = semantic_eval.load_cases()
    assert 20 <= len(cases) <= 30
    assert len({c["id"] for c in cases}) == len(cases)
    assert {c["group"] for c in cases} == set(semantic_eval.GROUPS)
    assert {c["trap"] for c in cases if c.get("trap")} == set(semantic_eval.TRAPS)


def test_load_cases_rejects_duplicates_and_bad_gold(tmp_path):
    good = json.loads(semantic_eval.CASES.read_text(encoding="utf-8"))
    for mutate, message in ((lambda cs: cs.append(dict(cs[0])), "duplicate"),
                            (lambda cs: cs[0]["expected"].update(intent="recommend"), "intent"),
                            (lambda cs: cs[0].update(forbidden_filters=["vrr"]), "not a contract field")):
        doc = json.loads(json.dumps(good))
        mutate(doc["cases"])
        path = tmp_path / "cases.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            semantic_eval.load_cases(path)


def test_compare_filters_counts_invented_items():
    f = semantic_eval.compare_filters({"panel_technology": ["OLED"], "max_price": 150000},
                                      {"panel_technology": ["OLED", "QLED"], "max_price": 15000, "min_refresh_rate_hz": 120})
    assert f["invented"] == ["min_refresh_rate_hz=120", "panel_technology=QLED"]
    assert f["wrong_value"] == ["max_price: expected 150000, got 15000"] and f["missed"] == []


@pytest.fixture(scope="module")
def evaluation():
    return semantic_eval.run(out=None)


def test_gold_evaluation_has_zero_invented_hard_filters(evaluation):
    s = evaluation["summary"]
    assert s["parse_unavailable"] == 0
    assert s["invented_hard_filter_count"] == 0 and s["invented_hard_filter_rate"] == 0.0
    assert s["forbidden_filter_violations"] == 0 and s["filter_items_wrong_value"] == 0
    assert all(t["answer"] == "NO" for t in evaluation["false_constraint_traps"])


def test_gold_evaluation_regression_lock(evaluation):
    s = evaluation["summary"]
    assert s["cases_passed"] == s["cases"] == 30, s["failed_cases"]


def test_shadow_calls_pass_the_real_tool_boundary(evaluation):
    for row in evaluation["shadow"]:
        assert row["valid"] in (True, None), row
        assert "required_features" not in row["args"]


def test_guard_flags_the_two_known_4d2e_inventions(evaluation):
    g = evaluation["guard_4d2e"]
    flagged = {f["turn"]: f["flags"] for f in g["flagged_turns"]}
    assert set(g["manual_fail_turns"]) <= set(flagged)
    assert flagged["rec-gaming[0]"] == ["recommend_tvs.required_features=hdmi_2_1"]
    assert flagged["followup-oled65-spike[2]"] == ["recommend_tvs.max_price=150000"]


# ---- isolation ------------------------------------------------------------------------------

@pytest.mark.parametrize("module", ["agent_tools", "agent_payload", "mcp_server", "n8n_workflow", "planning",
                                    "router", "retrieval", "ranking", "evidence", "features", "extract"])
def test_runtime_does_not_use_the_spike(module):
    source = (ROOT / "consultant" / f"{module}.py").read_text(encoding="utf-8")
    assert "query_semantics" not in source
