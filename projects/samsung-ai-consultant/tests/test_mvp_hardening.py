"""Phase 4F.3 MVP hardening: focused regressions for the three root causes (docs/PHASE_4F_3_MVP_HARDENING.md).

MVP-1  a feature that was asked about always has an explicit three-state value in the evidence;
MVP-2  a relative or vague request never leaves an invented number in the arguments the Core receives;
MVP-3  the aggregate tool contract: feature counts and series counts exist, and a count states its scope.

No DB, no network: the catalog is the 31-product fixture. The same contracts run end to end on a disposable
database in tests/test_mvp_hardening_db.py."""

import logging

import pytest

from consultant import agent_tools
from consultant.agent_payload import COUNT_SCOPE_NOTE, counted_scope, stats_payload
from consultant.agent_tools import (
    FEATURE_IDS, SUMMARY_FEATURE_IDS, TOOL_SCHEMAS, ConsultantTools, ToolArgumentError, add_feature_evidence, run_tool,
    validate_arguments,
)
from consultant.features import FEATURES, evaluate_features
from consultant.mcp_server import ConversationStore, guard_context
from consultant.n8n_workflow import PROMPT_FILE
from consultant.query_semantics import parse_query_semantics
from consultant.semantic_guard import MessageEvidence, guard_tool_arguments, not_applied_note, spelled_numbers

from .consultant_fixtures import product_rows

PS5 = "Посоветуйте телевизор под PS5, сын в основном в шутеры играет."
GAMING = ("hz_120", "vrr", "freesync_premium", "allm", "game_bar")
TVS = ("QE42S90HAEXPY", "QE48S85HAEXPY", "QE55QN80HAUXPY", "MRE55R85HAUXPY", "QE55S85HAEXPY", "QE65QN80HAUXPY")


class FixtureRepo:
    """The two read methods ``add_feature_evidence`` uses, over the curated production fixture."""

    def __init__(self):
        self.rows, self.specs, self.by_code = product_rows()
        self.calls = 0

    def get_products_by_codes(self, codes):
        self.calls += 1
        return [self.by_code[c] for c in codes if c in self.by_code]

    def get_specs(self, ids, names=None):
        return {i: [s for s in self.specs[i] if names is None or s.spec_name in names] for i in ids}

    def state(self, code: str, feature: str) -> str:
        row = self.by_code[code]
        return evaluate_features([row], self.specs, [feature])[row.id][feature].state.value


def result(repo: FixtureRepo, tool: str, codes=TVS, features=()) -> dict:
    """A tool result as the pipeline returns it: products with the states of the plan's features only."""
    products = []
    for n, code in enumerate(codes, start=1):
        row = repo.by_code[code]
        view = {"ref": f"P{n}", "model_code": code, "name": row.name, "current_price_rub": row.effective_price,
                "available": row.is_available, "specs": {"refresh_rate_hz": row.refresh_rate_hz}}
        if features:
            view["features"] = {f: repo.state(code, f) for f in features}
        products.append(view)
    return {"contract": "agent-result-v2", "tool": tool, "status": "ok", "confidence": "strong", "confidence_notes": [],
            "request": {}, "products": products, "gaps": []}


@pytest.fixture
def repo():
    return FixtureRepo()


@pytest.fixture
def served(monkeypatch, repo):
    """``ConsultantTools`` over the fixture with the pipeline replaced by a recorder: ``served.next`` is the result
    the Core would return; ``served.seen`` is what reached the tool boundary."""
    class Served:
        seen: list = []
        next: dict = {}
        tools = ConsultantTools.for_repository(repo)

    def fake_run_tool(name, arguments, _repo):
        Served.seen.append((name, arguments))
        return Served.next
    Served.seen = []
    monkeypatch.setattr(agent_tools, "run_tool", fake_run_tool)
    return Served


def states(payload: dict, feature: str) -> dict:
    return {p["model_code"]: p.get("features", {}).get(feature) for p in payload["products"]}


# ---- MVP-1: unknown is shown as unknown ------------------------------------------------------------------

def test_removed_requirement_is_reported_as_unknown_instead_of_disappearing(served, repo):
    """4F.2 PA-02 turn 1: the Agent's call, the guard's removal, and what the Agent now gets back."""
    served.next = result(repo, "recommend_tvs", features=GAMING)
    before = [dict(p, features=dict(p["features"])) for p in served.next["products"]]
    payload = served.tools.call("recommend_tvs", {"use_cases": ["gaming"], "required_features": ["hdmi_2_1", "hz_120"],
                                                  "preferred_features": ["allm", "vrr"]},
                                "t1", (MessageEvidence.from_text(PS5),))
    assert served.seen == [("recommend_tvs", {"use_cases": ["gaming"], "preferred_features": ["allm", "vrr"]})]
    assert set(states(payload, "hdmi_2_1").values()) == {"not_listed"}            # explicit for every product
    assert payload["request"]["features_checked"] == ["hz_120", "hdmi_2_1"]
    assert payload["request"]["not_applied"]["constraints"] == [{"argument": "required_features", "value": "hdmi_2_1"},
                                                                {"argument": "required_features", "value": "hz_120"}]
    gap = next(g for g in payload["gaps"] if g["attributes"] == ["hdmi_2_1"])
    assert gap["kind"] == "attribute_not_listed_for_product" and gap["products"] == [p["ref"] for p in payload["products"]]
    assert "neither 'yes' nor 'no'" in gap["detail"]
    assert payload["confidence"] == "partial" and payload["confidence_notes"]
    # nothing else moved: same products, same order, same prices, same states for the plan's features
    for old, new in zip(before, payload["products"]):
        assert {k: new[k] for k in old if k != "features"} == {k: old[k] for k in old if k != "features"}
        assert {f: new["features"][f] for f in old["features"]} == old["features"]


def test_a_feature_the_user_names_is_reported_even_when_the_agent_does_not_pass_it(served, repo):
    served.next = result(repo, "get_tv", codes=TVS[:1])                           # an overview: no attributes asked
    payload = served.tools.call("get_tv", {"model": TVS[0]}, "t2",
                                (MessageEvidence.from_text(PS5),
                                 MessageEvidence.from_text("А HDMI 2.1 у них точно есть? Без него 120 кадров не будет.")))
    features = payload["products"][0]["features"]
    assert features["hdmi_2_1"] == "not_listed" and features["hz_120"] == {"state": "yes", "value": 120}
    assert "feature_summary" not in payload                                       # only list results are summarized


def test_an_explicit_negative_stays_a_negative(served, repo):
    served.next = result(repo, "search_tvs", codes=("UE32H5000FUXRU", "QE42S90HAEXPY"))
    payload = served.tools.call("search_tvs", {}, "t1", (MessageEvidence.from_text("нужен телевизор, 120 Гц обязательно"),))
    assert states(payload, "hz_120") == {"UE32H5000FUXRU": {"state": "no", "value": 60},
                                         "QE42S90HAEXPY": {"state": "yes", "value": 120}}


MENTIONS = [("нужен VRR", "vrr"), ("обязательно HDMI 2.1", "hdmi_2_1"), ("120 Гц обязательно", "hz_120"),
            ("чтобы был ALLM", "allm"), ("FreeSync Premium Pro", "freesync_premium_pro"), ("а FreeSync есть?", "freesync_premium"),
            ("нужен Game Bar", "game_bar"), ("Dolby Atmos обязательно", "dolby_atmos"), ("eARC для саундбара", "earc"),
            ("антибликовое покрытие", "anti_glare"), ("режим Filmmaker", "filmmaker_mode"),
            ("мощность звука от 40 Вт", "sound_power_w"), ("какая у него толщина", "depth_cm"), ("крепление VESA", "vesa")]


def test_the_mention_table_covers_the_whole_registry():
    assert {f for _, f in MENTIONS} == set(FEATURES)


@pytest.mark.parametrize("text, feature", MENTIONS)
def test_a_requested_feature_never_becomes_yes_without_catalog_evidence(served, repo, text, feature):
    """Generic over the registry: asking for a feature adds its catalog state, and that state is the registry's
    own evaluation for every product -- ``yes`` only where the catalog says so."""
    codes = [r.model_code for r in repo.rows]
    served.next = result(repo, "search_tvs", codes=codes)
    payload = served.tools.call("search_tvs", {}, "t1", (MessageEvidence.from_text(text),))
    got = {code: (s if isinstance(s, str) else s["state"]) for code, s in states(payload, feature).items()}
    assert got == {code: repo.state(code, feature) for code in codes}
    assert "not_listed" in got.values() or feature in ("hz_120", "sound_power_w")     # the fixture has unknowns
    unknown = sorted(p["ref"] for p in payload["products"] if got[p["model_code"]] == "not_listed")
    listed = sorted(ref for g in payload["gaps"] if g.get("attributes") == [feature] for ref in g["products"])
    assert listed == unknown                                                          # every unknown is a stated gap


def test_list_results_summarize_every_boolean_registry_feature(repo):
    payload = add_feature_evidence(result(repo, "recommend_tvs", features=GAMING), repo)
    summary = payload["feature_summary"]
    assert list(payload).index("feature_summary") + 1 == list(payload).index("products")
    assert set(summary["counts"]) == set(SUMMARY_FEATURE_IDS) == {f for f, d in FEATURES.items() if d.kind == "bool"}
    assert summary["products"] == len(TVS) and all(sum(c.values()) == len(TVS) for c in summary["counts"].values())
    assert summary["counts"]["hdmi_2_1"] == {"not_listed": len(TVS)}              # unknown for all: stated, not omitted
    assert summary["counts"]["vrr"] == {"yes": len(TVS)}
    assert "not the catalog" in summary["note"] and "unknown" in summary["note"]
    # the two lists an introduction is written from: what every shown product has, and what the catalog does not say
    assert summary["all_yes"] == [f for f in SUMMARY_FEATURE_IDS if summary["counts"][f] == {"yes": len(TVS)}]
    assert "vrr" in summary["all_yes"] and "hdmi_2_1" not in summary["all_yes"] and "allm" not in summary["all_yes"]
    assert "hdmi_2_1" in summary["not_listed_for_all"] and not set(summary["all_yes"]) & set(summary["not_listed_for_all"])
    assert list(summary) == ["note", "products", "all_yes", "not_listed_for_all", "counts"]
    for code, state in states(payload, "hdmi_2_1").items():
        assert state is None                                                      # not asked: per-product set unchanged
    assert "feature_summary" not in add_feature_evidence(result(repo, "compare_tvs"), repo)
    assert "feature_summary" not in add_feature_evidence(result(repo, "search_tvs", codes=()), repo)


def test_nothing_is_added_without_products_or_without_an_asked_feature(repo):
    empty = {"tool": "get_tv", "status": "not_found", "products": [], "gaps": []}
    assert add_feature_evidence(dict(empty), repo, ("hdmi_2_1",)) == empty and repo.calls == 0
    plain = result(repo, "get_tv", codes=TVS[:1])
    assert add_feature_evidence(result(repo, "get_tv", codes=TVS[:1]), repo) == plain and repo.calls == 0


def test_alternatives_get_the_asked_states_too(repo):
    payload = result(repo, "recommend_tvs", codes=())
    payload.update(status="no_match", alternatives=result(repo, "recommend_tvs", codes=TVS[:2])["products"])
    out = add_feature_evidence(payload, repo, ("hdmi_2_1",))
    assert [a["features"]["hdmi_2_1"] for a in out["alternatives"]] == ["not_listed", "not_listed"]
    assert "feature_summary" not in out                                           # there are no matching products


def test_feature_evidence_failure_leaves_the_plain_result(served, repo, monkeypatch, caplog):
    served.next = result(repo, "recommend_tvs", features=GAMING)
    plain = result(repo, "recommend_tvs", features=GAMING)
    monkeypatch.setattr(repo, "get_products_by_codes", lambda codes: 1 / 0)
    with caplog.at_level(logging.ERROR):
        payload = served.tools.call("recommend_tvs", {"use_cases": ["gaming"]}, "t1", (MessageEvidence.from_text(PS5),))
    assert payload == plain and "feature evidence unavailable" in caplog.text


def test_without_guard_context_per_product_features_are_untouched(served, repo):
    served.next = result(repo, "get_tv", codes=TVS[:1], features=("vrr",))
    payload = served.tools.call("get_tv", {"model": TVS[0], "attributes": ["vrr"]}, "t1")
    assert payload["products"][0]["features"] == {"vrr": "yes"} and "features_checked" not in payload["request"]


def test_not_applied_note_says_the_feature_was_not_required():
    g = guard_tool_arguments("recommend_tvs", {"use_cases": ["gaming"], "required_features": ["hdmi_2_1"]}, [PS5])
    note = not_applied_note(g)["note"]
    assert "Do not present the results as satisfying these constraints" in note
    assert "a product has it only if its `features` says yes" in note


# ---- MVP-2: relative and vague words never become numbers ---------------------------------------------------

BEDROOM = ["здрасте, хочу норм телек в спальню, не огромный, и чтоб картинка была вау",
           "ну дюймов 43-50, до сотки где-то", "а что подешевле есть?"]


def test_4f2_pa08_invented_ceiling_no_longer_reaches_the_core():
    """The recorded 4F.2 call. Before 4F.3 the slang number «сотки» switched the numeric rules off."""
    args = {"max_price": 40000, "min_screen_size_inches": 40, "max_screen_size_inches": 50, "availability": "available",
            "sort": "price_asc", "limit": 3}
    g = guard_tool_arguments("search_tvs", args, BEDROOM)
    assert g.status == "modified"
    assert [(a.argument, a.value, a.reason) for a in g.removed] == [("max_price", 40000, "unsupported_price_value"),
                                                                    ("min_screen_size_inches", 40, "unsupported_size_value")]
    assert "max_price" not in g.arguments and g.arguments["max_screen_size_inches"] == 50
    assert not_applied_note(g)["constraints"][0] == {"argument": "max_price", "value": 40000}


def test_4f2_pa08_stated_limits_are_kept_with_the_relative_request():
    """What the Agent should send for «подешевле»: the user's own limits and an order. Nothing is removed."""
    args = {"min_screen_size_inches": 43, "max_screen_size_inches": 50, "max_price": 100000, "sort": "price_asc"}
    g = guard_tool_arguments("search_tvs", args, BEDROOM)
    assert g.status == "unchanged" and g.arguments == args


def _turn(seen, store, conv, n, message, tool, args):
    """One user turn through the real path: guard_context -> ConversationStore -> ConsultantTools.call -> guard."""
    seen.clear()
    conversation, act = guard_context(store, f"{conv}-t{n}", conv, message, n - 1)
    ConsultantTools.for_repository(object()).call(tool, args, f"{conv}-t{n}", conversation, act)
    return seen[0][1]


def test_explicit_budget_stays_and_a_cheaper_follow_up_adds_no_threshold(served):
    served.next = {"contract": "agent-result-v2", "tool": "search_tvs", "status": "ok", "request": {}, "products": []}
    store = ConversationStore()
    first = {"max_price": 100000}
    assert _turn(served.seen, store, "c", 1, "Покажи телевизоры до 100 тысяч.", "search_tvs", first) == first
    carried = {"max_price": 100000, "sort": "price_asc"}
    assert _turn(served.seen, store, "c", 2, "а что подешевле?", "search_tvs", carried) == carried       # budget active
    invented = {"max_price": 60000, "sort": "price_asc"}
    assert _turn(served.seen, store, "c", 2, "а что подешевле?", "search_tvs", invented) == {"sort": "price_asc"}
    assert _turn(served.seen, store, "c", 3, "а ещё дешевле?", "recommend_tvs",
                 {"max_price": 100000, "min_price": 20000}) == {"max_price": 100000}                 # only the invention goes


@pytest.mark.parametrize("tool, phrase, args, kept", [
    ("recommend_tvs", "хочу не огромный телевизор", {"max_screen_size_inches": 55}, {}),
    ("recommend_tvs", "нужен небольшой телевизор на кухню", {"max_screen_size_inches": 32, "use_cases": ["compact"]},
     {"use_cases": ["compact"]}),
    ("search_tvs", "а что есть подороже?", {"min_price": 150000}, {}),
    ("search_tvs", "а побольше есть?", {"min_screen_size_inches": 75}, {}),
    ("search_tvs", "а чуть дешевле?", {"max_price": 90000}, {}),
    ("recommend_tvs", "нужен бюджетный телевизор", {"max_price": 30000}, {}),
    ("search_tvs", "покажи не самый дорогой", {"max_price": 200000, "sort": "price_desc"}, {"sort": "price_desc"}),
    ("get_catalog_stats", "а подешевле?", {"stat": "cheapest", "max_price": 50000, "min_refresh_rate_hz": 60}, {"stat": "cheapest"}),
])
def test_vague_and_relative_words_leave_no_number_in_the_arguments(tool, phrase, args, kept):
    g = guard_tool_arguments(tool, args, ["Посоветуй телевизор.", phrase])
    assert g.status == "modified" and g.arguments == kept
    filters = parse_query_semantics(phrase).semantics.filters.to_dict()
    assert not {"min_price", "max_price", "screen_size_inches", "min_screen_size_inches", "max_screen_size_inches"} & set(filters)


@pytest.mark.parametrize("text, args", [
    ("до 100 тысяч", {"max_price": 100000}),
    ("65 дюймов", {"screen_size_inches": 65}),
    ("не дороже 80 000", {"max_price": 80000}),
    ("от 100 до 150 тысяч, не меньше 55 дюймов", {"min_price": 100000, "max_price": 150000, "min_screen_size_inches": 55}),
    ("до сотки", {"max_price": 100000}),
    ("бюджет сто тысяч", {"max_price": 100000}),
    ("до ста пятидесяти тысяч", {"max_price": 150000}),
    ("не больше полутора миллионов", {"max_price": 1500000}),
    ("полтинник максимум", {"max_price": 50000}),
    ("в пределах двухсот тысяч, дюймов шестьдесят пять", {"max_price": 200000, "screen_size_inches": 65}),
])
def test_explicit_numeric_constraints_are_kept(text, args):
    g = guard_tool_arguments("search_tvs", args, [text])
    assert g.status == "unchanged" and g.arguments == args
    later = guard_tool_arguments("search_tvs", {**args, "sort": "price_asc"}, [text, "а что подешевле?"])
    assert later.status == "unchanged"                                            # still active in the follow-up


@pytest.mark.parametrize("text", ["до сотки", "бюджет сто тысяч", "до ста пятидесяти тысяч", "полтинник максимум"])
def test_a_spelled_budget_no_longer_disables_the_protection(text):
    g = guard_tool_arguments("search_tvs", {"max_price": 37000}, [text, "а что подешевле?"])
    assert g.status == "modified" and g.arguments == {}


@pytest.mark.parametrize("text, values", [
    ("до сотки где-то", {100}), ("сто тысяч", {100000}), ("до ста пятидесяти тысяч", {150000}),
    ("сто двадцать пять тысяч пятьсот", {125500}), ("полтора миллиона", {1500000}), ("до полутора миллионов", {1500000}),
    ("две сотки", {200}), ("полторы сотни", {150}), ("полтинник", {50}), ("пять косарей", {5000}), ("лям", {1000000}),
    ("тысяча двести", {1200}), ("миллион двести тысяч", {1200000}), ("полмиллиона", {500000}),
    ("от ста до двухсот тысяч", {100, 200000}), ("сорок три дюйма", {43}), ("девятнадцать", {19}),
])
def test_spelled_numbers_are_read(text, values):
    assert spelled_numbers(text) == (values, False)
    assert {v * 1.0 for v in values} <= set(MessageEvidence.from_text(text).numbers)


@pytest.mark.parametrize("text", [
    "пара сотен", "несколько тысяч", "тысяч двести", "сто-двести тысяч", "полторы-две тысячи", "два с половиной миллиона",
    "2 сотки", "пол миллиона", "сто сто", "двадцать десять", "сотен", "тысяч миллион", "полторашка",
])
def test_number_words_outside_the_grammar_keep_the_old_fail_open_behaviour(text):
    """Not read with certainty -> the numeric rules stay off for the conversation, exactly as before 4F.3: an
    explicit budget in that form is never removed (and neither is an invention: the documented limit)."""
    assert spelled_numbers(text)[1] is True
    assert guard_tool_arguments("search_tvs", {"max_price": 200000}, [text]).status == "unchanged"


@pytest.mark.parametrize("text", ["сколько стоит этот телевизор", "150 тысяч", "до 150к", "0,15 млн", "ну дюймов 43-50",
                                  "а что подешевле есть?", "высота 70 см", "хочу стол и стену"])
def test_ordinary_text_and_digit_amounts_are_not_number_words(text):
    assert spelled_numbers(text) == (set(), False)


# ---- MVP-3: the aggregate contract ------------------------------------------------------------------------

def test_stats_schema_counts_features_and_series():
    props = TOOL_SCHEMAS["get_catalog_stats"]["inputSchema"]["properties"]
    assert set(props["attributes"]["items"]["enum"]) == set(FEATURE_IDS) and "model" in props
    assert props["group_by"]["enum"] == ["panel_technology", "category", "screen_size_inches", "refresh_rate_hz"]
    for args in ({"stat": "count", "availability": "available", "attributes": ["dolby_atmos"]},       # the 4F.2 PA-15 call
                 {"stat": "count", "model": "QN70H"}, {"stat": "count", "panel_technology": ["OLED"], "attributes": ["hz_120", "vrr"]},
                 {"stat": "count", "max_price": 50000, "group_by": "refresh_rate_hz"}):
        assert validate_arguments("get_catalog_stats", args) == args
    description = TOOL_SCHEMAS["get_catalog_stats"]["description"]
    assert "pass `attributes`" in description and "pass `model`" in description
    assert "get_catalog_stats" in TOOL_SCHEMAS["search_tvs"]["description"] and "filtered selection" in \
        TOOL_SCHEMAS["search_tvs"]["description"]


@pytest.mark.parametrize("args, message", [
    ({"stat": "cheapest", "attributes": ["vrr"]}, "attributes: only valid with stat='count'"),
    ({"stat": "largest", "model": "S95H"}, "model: only valid with stat='count'"),
    ({"stat": "count", "attributes": ["vrr"], "group_by": "category"}, "cannot be combined with group_by"),
    ({"stat": "count", "attributes": ["brightness_nits"]}, "not in"),
    ({"stat": "count", "attributes": []}, "at least 1"),
    ({"stat": "count", "model": "QN70H'; DROP"}, "malformed"),
    ({"stat": "count", "required_features": ["dolby_atmos"]}, "unknown field"),
])
def test_stats_argument_errors_are_reported_not_ignored(args, message):
    payload = run_tool("get_catalog_stats", args, None)
    assert payload["status"] == "invalid_arguments" and message in payload["errors"][0]


def test_a_count_states_what_was_counted():
    class Plan:
        filters = None
        user_constraint_keys = ()
        resolutions = ()
        gaps = ()
    payload = stats_payload(Plan, {"available": 66}, None, [], {})
    assert payload["counted"] == counted_scope(Plan) == "all products in the catalog"
    assert payload["scope_note"] == COUNT_SCOPE_NOTE and "attribute_counts" not in payload
    assert "not counts of products with any feature" in COUNT_SCOPE_NOTE.replace("They are ", "")
    with_features = stats_payload(Plan, {"available": 66}, None, [], {},
                                  {"dolby_atmos": {"available": {"yes": 54, "no": 0, "not_listed": 12}}})
    assert list(with_features).index("counts") + 1 == list(with_features).index("attribute_counts")
    assert sum(with_features["attribute_counts"]["dolby_atmos"]["available"].values()) == with_features["counts"]["available"]
    assert "attribute_values" not in with_features                                # only attributes that carry a value
    rates = stats_payload(Plan, {"total": 10}, None, [], {}, {"hz_120": {"total": {"yes": 0, "no": 10, "not_listed": 0}}},
                          {"hz_120": {"total": {"50": 2, "60": 8}}})
    assert list(rates).index("attribute_counts") + 1 == list(rates).index("attribute_values")
    assert "never implied by a 'no'" in COUNT_SCOPE_NOTE and "`attribute_values`" in COUNT_SCOPE_NOTE


# ---- prompt v4 ----------------------------------------------------------------------------------------------

def test_prompt_v4_adds_the_three_rules_without_scenario_phrasing():
    p = PROMPT_FILE.read_text(encoding="utf-8")
    assert PROMPT_FILE.name == "agent_system_v4.md"
    unknown = next(line for line in p.splitlines() if line.startswith("- Unknown is not yes."))
    for needle in ("only when a tool result of this conversation shows it for that product",
                   "general knowledge, not a catalog fact", "`feature_summary`", "`request.not_applied` was not checked",
                   "a sentence about all the shown models names only features from its `all_yes`"):
        assert needle in unknown, needle
    relative = next(line for line in p.splitlines() if line.startswith("- Relative and vague words"))
    for needle in ("comparisons, not numbers", "never become max_price, min_price or a size",
                   "Pass every limit the user already stated exactly as stated, add no new one", "sort price_asc"):
        assert needle in relative, needle
    groups = next(line for line in p.splitlines() if line.startswith("- Counts and statements about a whole group"))
    for needle in ("come only from get_catalog_stats, never from a list", "filtered selection", "`counted` scope",
                   "`attribute_counts`", "never present a total or an availability count as the number of models with a feature",
                   "`attribute_values`", "never derive a value from a `no`",
                   "this catalog, not about all Samsung models"):
        assert needle in groups, needle
    assert "call get_tv with `question`" in p and "A general overview is not the whole record" in p
    # the rules are general: the new lines name no feature, and no acceptance or demo phrasing is an example
    assert "HDMI" not in unknown + relative + groups and "Atmos" not in unknown + relative + groups
    for held_out in ("сотк", "не огромный", "что подешевле есть", "в шутеры", "QN70H", "66 ", "40 000", "до 50 тысяч",
                     "у всех 120", "Wi-Fi", "вайфай"):
        assert held_out not in p, held_out
    assert ('stays out of every list of what they support — also with a remark in brackets — and is never described as '
            'something such models usually have') in unknown and '"В каталоге нет данных о … для этих моделей."' in unknown
    prices = next(line for line in p.splitlines() if line.startswith("- Prices:"))
    assert "copy the price from the `price_text` of that same model code, never from another product" in prices
    # every v3 rule is kept: verbatim, or as the start of a v4 line that adds to it (only the stats tool line was reworded)
    v3 = (PROMPT_FILE.parent / "agent_system_v3.md").read_text(encoding="utf-8")
    assert [line for line in v3.splitlines() if not any(new.startswith(line) for new in p.splitlines())] == [
        next(line for line in v3.splitlines() if line.startswith("  - get_catalog_stats"))]


def test_every_product_carries_its_price_as_text_next_to_its_model_code():
    """Targeted run 2 and the rate measurement: QE55QN80HAUXPY was given the price of QE65QN80HAUXPY in 2 of 14
    answers. The price is now one string with the model code it belongs to, already in the answer's format."""
    from consultant.agent_payload import price_text
    assert price_text("QE55QN80HAUXPY", 129990) == "QE55QN80HAUXPY: 129 990 ₽"
    assert price_text("QE65S85HAEXPY", 189990, 229990) == "QE65S85HAEXPY: 189 990 ₽ (без скидки 229 990 ₽)"
    assert price_text("QE115QN90FUXRU", 1799990, 1999990) == "QE115QN90FUXRU: 1 799 990 ₽ (без скидки 1 999 990 ₽)"
    assert price_text("X", None) is None
