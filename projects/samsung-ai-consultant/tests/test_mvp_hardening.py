"""Phase 4F.3 MVP hardening: focused regressions for the three root causes (docs/PHASE_4F_3_MVP_HARDENING.md).

MVP-1  a feature that was asked about always has an explicit three-state value in the evidence;
MVP-2  a relative or vague request never leaves an invented number in the arguments the Core receives;
       Phase 4F.3A: such a call is rejected with ``invalid_arguments`` instead of being run without the number;
MVP-3  the aggregate tool contract: feature counts and series counts exist, and a count states its scope.

No DB, no network: the catalog is the 31-product fixture. The same contracts run end to end on a disposable
database in tests/test_mvp_hardening_db.py."""

import logging

import pytest

from consultant import agent_tools
from consultant.agent_payload import COUNT_SCOPE_NOTE, counted_scope, stats_payload
from consultant.agent_tools import (
    FEATURE_IDS, TOOL_SCHEMAS, ConsultantTools, ToolArgumentError, add_feature_evidence, run_tool, validate_arguments,
)
from consultant.features import FEATURES, evaluate_features
from consultant.mcp_server import ConversationStore, guard_context, handle_message
import hashlib
import json

from consultant.n8n_workflow import PROMPT_FILE
from consultant.query_semantics import parse_query_semantics
from consultant.semantic_guard import (
    NUMERIC_ARGS, MessageEvidence, guard_tool_arguments, not_applied_note, reject_unsupported_numeric, spelled_numbers,
)

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
    # unknown for every returned product: said once more where the result's caveats are, as for a not-listed lookup
    assert payload["confidence"] == "partial" and len(payload["confidence_notes"]) == 1
    note = payload["confidence_notes"][0]
    assert "hdmi_2_1" in note and "hz_120" not in note and "neither 'yes' nor 'no'" in note and "no data" in note
    assert set(payload) == set(result(repo, "recommend_tvs"))                         # no new top-level field
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


def test_the_caveat_note_is_only_for_a_feature_unknown_for_every_returned_product(served, repo):
    served.next = result(repo, "recommend_tvs", codes=("QE65S95HAUXPY", "MNA114MS1CCXRU"))    # the display lists its HDMI version
    mixed = served.tools.call("recommend_tvs", {}, "t1", (MessageEvidence.from_text("обязательно HDMI 2.1"),))
    assert states(mixed, "hdmi_2_1") == {"QE65S95HAUXPY": "not_listed", "MNA114MS1CCXRU": "yes"}
    assert mixed["confidence"] == "strong" and mixed["confidence_notes"] == []                # per product only
    assert [g["products"] for g in mixed["gaps"]] == [["P1"]]
    served.next = {**result(repo, "recommend_tvs"), "confidence": "weak", "confidence_notes": ["a sample, not a ranking"]}
    weak = served.tools.call("recommend_tvs", {}, "t2", (MessageEvidence.from_text("обязательно HDMI 2.1"),))
    assert weak["confidence"] == "weak" and len(weak["confidence_notes"]) == 2                # never upgraded; note appended


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


def test_a_result_is_untouched_when_no_feature_was_asked_about(served, repo):
    """The step adds nothing by default: an ordinary recommendation or list is exactly what the pipeline returned.
    (A list-wide feature summary and a pre-written price text were tried and removed in this phase.)"""
    for tool in ("recommend_tvs", "search_tvs", "compare_tvs", "get_tv"):
        served.next = result(repo, tool, features=GAMING)
        untouched = result(repo, tool, features=GAMING)
        assert served.tools.call(tool, {}, f"t-{tool}", (MessageEvidence.from_text("Посоветуйте телевизор для игр."),)) == untouched
        assert add_feature_evidence(result(repo, tool, features=GAMING), repo) == untouched
    assert repo.calls == 0
    for p in untouched["products"]:
        assert "price_text" not in p
    assert "feature_summary" not in untouched


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


def test_feature_evidence_failure_leaves_the_plain_result(served, repo, monkeypatch, caplog):
    served.next = result(repo, "recommend_tvs", features=GAMING)
    plain = result(repo, "recommend_tvs", features=GAMING)
    monkeypatch.setattr(repo, "get_products_by_codes", lambda codes: 1 / 0)
    with caplog.at_level(logging.ERROR):
        payload = served.tools.call("recommend_tvs", {"use_cases": ["gaming"]}, "t1",
                                    (MessageEvidence.from_text(PS5), MessageEvidence.from_text("А HDMI 2.1 у них есть?")))
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
    """One user turn through the real path: guard_context -> ConversationStore -> ConsultantTools.call -> guard.
    Returns the arguments the Core received; ``None`` when the call was rejected (Phase 4F.3A)."""
    seen.clear()
    conversation, act = guard_context(store, f"{conv}-t{n}", conv, message, n - 1)
    ConsultantTools.for_repository(object()).call(tool, args, f"{conv}-t{n}", conversation, act)
    return seen[0][1] if seen else None


def test_explicit_budget_stays_and_a_cheaper_follow_up_adds_no_threshold(served):
    served.next = {"contract": "agent-result-v2", "tool": "search_tvs", "status": "ok", "request": {}, "products": []}
    store = ConversationStore()
    first = {"max_price": 100000}
    assert _turn(served.seen, store, "c", 1, "Покажи телевизоры до 100 тысяч.", "search_tvs", first) == first
    carried = {"max_price": 100000, "sort": "price_asc"}
    assert _turn(served.seen, store, "c", 2, "а что подешевле?", "search_tvs", carried) == carried       # budget active
    invented = {"max_price": 60000, "sort": "price_asc"}
    assert _turn(served.seen, store, "c", 2, "а что подешевле?", "search_tvs", invented) is None         # rejected, not run
    assert _turn(served.seen, store, "c", 3, "а ещё дешевле?", "recommend_tvs",
                 {"max_price": 100000, "min_price": 20000}) is None                                  # the invented minimum
    assert _turn(served.seen, store, "c", 3, "а ещё дешевле?", "recommend_tvs",
                 {"max_price": 100000}) == {"max_price": 100000}                                     # the stated budget runs


# ---- Phase 4F.3A: an unsupported number rejects the call; the Agent retries ----------------------------------
# 4F.3 final runs: the guard removed the invented number and the Core ran without it, but the model had seen its
# own call succeed and worded the result by that number («до 50 000 ₽», «до 55 дюймов»).

OK_RESULT = {"contract": "agent-result-v2", "tool": "search_tvs", "status": "ok", "request": {}, "products": []}
CHEAPER = ("Нужен телевизор 55 дюймов до 100 тысяч, в основном для кино и сериалов.", "А есть что-то подешевле?")


def call(served, messages, tool, args, turn="t1", act=True):
    """``(result the Agent gets, arguments the Core received or None)`` for one tool call of a turn."""
    served.seen.clear()
    served.next = dict(OK_RESULT, request={})
    payload = served.tools.call(tool, args, turn, tuple(MessageEvidence.from_text(m) for m in messages), act)
    return payload, (served.seen[0][1] if served.seen else None)


def rejected(payload: dict) -> list:
    assert payload["status"] == "invalid_arguments" and "products" not in payload
    return [(c["argument"], c["value"]) for c in payload["unsupported_numeric_constraints"]]


def test_4f3a_an_explicit_budget_is_accepted(served):
    args = {"max_price": 100000}
    payload, core = call(served, ["до 100 тысяч"], "search_tvs", args)
    assert payload["status"] == "ok" and core == args and "unsupported_numeric_constraints" not in payload


def test_4f3a_a_relative_price_request_rejects_an_invented_budget(served):
    """DEMO-04 turn 2, the recorded proposal."""
    payload, core = call(served, CHEAPER, "search_tvs", {"screen_size_inches": 55, "max_price": 50000, "sort": "price_asc"})
    assert core is None                                                            # nothing was run without it
    assert rejected(payload) == [("max_price", 50000)]
    assert payload["errors"][0] == "unsupported_numeric_constraint: max_price=50000 was not stated by the user in this conversation."
    retry = payload["errors"][-1]
    assert "was not run" in retry and "Call the tool again without" in retry and "Do not substitute another number" in retry
    assert "the answer must not state one" in retry


def test_4f3a_a_vague_size_rejects_an_invented_size(served):
    """PA-08 turn 1, the recorded proposal."""
    opener = "здрасте, хочу норм телек в спальню, не огромный, и чтоб картинка была вау"
    payload, core = call(served, [opener], "recommend_tvs", {"max_screen_size_inches": 55, "use_cases": ["movies"]})
    assert core is None and rejected(payload) == [("max_screen_size_inches", 55)]
    assert "max_screen_size_inches=55 was not stated by the user" in payload["errors"][0]


def test_4f3a_an_explicit_size_is_accepted(served):
    args = {"max_screen_size_inches": 55}
    payload, core = call(served, ["не больше 55 дюймов"], "recommend_tvs", args)
    assert payload["status"] == "ok" and core == args


def test_4f3a_a_rejected_call_can_be_followed_by_a_corrected_one(served):
    """The retry flow inside one user message: reject, then the same call without the invention runs."""
    invented = {"screen_size_inches": 55, "max_price": 50000, "sort": "price_asc"}
    corrected = {"screen_size_inches": 55, "max_price": 100000, "sort": "price_asc"}
    first, core = call(served, CHEAPER, "search_tvs", invented, turn="t2")
    assert rejected(first) == [("max_price", 50000)] and core is None
    second, core = call(served, CHEAPER, "search_tvs", corrected, turn="t2")
    assert second["status"] == "ok" and core == corrected and "not_applied" not in second["request"]
    # a second invention is rejected the same way; the rejections count towards the cap of three calls per message
    third, core = call(served, CHEAPER, "search_tvs", {"screen_size_inches": 55, "max_price": 45000}, turn="t2")
    assert rejected(third) == [("max_price", 45000)] and core is None
    assert call(served, CHEAPER, "search_tvs", corrected, turn="t2")[0]["status"] == "tool_call_limit_reached"


VALUES = {"min_price": 20000, "max_price": 50000, "screen_size_inches": 55, "min_screen_size_inches": 50,
          "max_screen_size_inches": 55, "min_refresh_rate_hz": 100}


def test_4f3a_every_numeric_hard_constraint_of_the_tools_is_covered():
    numeric = {k for k, v in TOOL_SCHEMAS["recommend_tvs"]["inputSchema"]["properties"].items()
               if v["type"] in ("number", "integer")}
    assert numeric == set(NUMERIC_ARGS) == set(VALUES)


@pytest.mark.parametrize("argument", sorted(VALUES))
@pytest.mark.parametrize("tool", ["search_tvs", "recommend_tvs", "get_catalog_stats"])
def test_4f3a_the_rule_is_generic_over_numeric_constraints(served, tool, argument):
    args = {argument: VALUES[argument], **({"stat": "count"} if tool == "get_catalog_stats" else {})}
    payload, core = call(served, ["Посоветуй телевизор.", "а что-нибудь получше?"], tool, args)
    assert core is None and rejected(payload) == [(argument, VALUES[argument])]
    payload, core = call(served, ["Посоветуй телевизор.", f"пусть будет {VALUES[argument]}"], tool, args, turn="t2")
    assert payload["status"] == "ok" and core == args                              # stated by the user: accepted


def test_4f3a_only_the_unsupported_numbers_are_named(served):
    """Stated limits and other arguments are not part of the rejection; an unmentioned required feature is still
    removed (and reported) on the call that runs."""
    args = {"min_screen_size_inches": 40, "max_screen_size_inches": 50, "max_price": 40000, "sort": "price_asc", "limit": 3}
    payload, core = call(served, BEDROOM, "search_tvs", args)
    assert core is None and rejected(payload) == [("max_price", 40000), ("min_screen_size_inches", 40)]
    assert len(payload["errors"]) == 3 and all(len(e) < 300 for e in payload["errors"])       # nothing is cut off
    assert "max_screen_size_inches" not in " ".join(payload["errors"])
    mixed = {"use_cases": ["gaming"], "required_features": ["hdmi_2_1"], "max_price": 150000}
    payload, core = call(served, [PS5], "recommend_tvs", mixed, turn="t2")
    assert core is None and rejected(payload) == [("max_price", 150000)] and "hdmi_2_1" not in str(payload)
    payload, core = call(served, [PS5], "recommend_tvs", {k: v for k, v in mixed.items() if k != "max_price"}, turn="t2")
    assert core == {"use_cases": ["gaming"]}
    assert payload["request"]["not_applied"]["constraints"] == [{"argument": "required_features", "value": "hdmi_2_1"}]


def test_4f3a_the_guards_own_decision_is_unchanged():
    """Rejection is derived from the same decision the replay records: ``modified`` with the number removed."""
    g = guard_tool_arguments("search_tvs", {"max_price": 50000, "sort": "price_asc"}, list(CHEAPER))
    assert g.status == "modified" and g.arguments == {"sort": "price_asc"}
    r = reject_unsupported_numeric(g)
    assert r.status == "rejected" and r.arguments is None and r.detail == "unsupported_numeric_constraint"
    assert [(a.action, a.argument, a.value, a.reason) for a in r.actions] == [("rejected", "max_price", 50000, "unsupported_price_value")]
    features_only = guard_tool_arguments("recommend_tvs", {"required_features": ["hdmi_2_1"]}, [PS5])
    assert features_only.status == "modified" and reject_unsupported_numeric(features_only) is None
    unchanged = guard_tool_arguments("search_tvs", {"max_price": 100000}, list(CHEAPER))
    assert unchanged.status == "unchanged" and reject_unsupported_numeric(unchanged) is None


def test_4f3a_no_rejection_when_the_guard_may_not_act(served):
    """Earlier messages unknown (report-only), or a number word the guard cannot read: the call runs as before."""
    args = {"max_price": 50000}
    payload, core = call(served, CHEAPER[1:], "search_tvs", args, act=False)
    assert payload["status"] == "ok" and core == args
    payload, core = call(served, ["бюджет пара сотен", "а что подешевле?"], "search_tvs", args, turn="t2")
    assert payload["status"] == "ok" and core == args
    served.seen.clear()
    assert served.tools.call("search_tvs", args, "t3")["status"] == "ok" and served.seen == [("search_tvs", args)]   # no context


def test_4f3a_a_failure_while_rejecting_falls_back_to_the_plain_call(served, monkeypatch, caplog):
    from consultant import semantic_guard
    monkeypatch.setattr(semantic_guard, "rejection_errors", lambda r: 1 / 0)
    with caplog.at_level(logging.ERROR):
        payload, core = call(served, CHEAPER, "search_tvs", {"max_price": 50000})
    assert payload["status"] == "ok" and core == {"max_price": 50000} and "semantic guard failed" in caplog.text


def test_4f3a_the_rejection_is_logged_and_returned_without_user_text(served, caplog):
    with caplog.at_level(logging.INFO, logger="consultant.agent_tools"):
        payload, _ = call(served, ["секретная фраза", "а что подешевле?"], "search_tvs", {"max_price": 50000})
    assert "'status': 'invalid_arguments'" in caplog.text and "'guard': 'rejected'" in caplog.text
    assert "['rejected', 'max_price', 50000, 'unsupported_price_value']" in caplog.text
    assert "секретная" not in caplog.text and "секретная" not in json.dumps(payload, ensure_ascii=False)


def test_4f3a_the_rejection_is_a_domain_result_over_mcp(served):
    """``isError`` stays false, as for every ``invalid_arguments``: the Agent reads the result and corrects the call."""
    served.next = dict(OK_RESULT, request={})
    message = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "search_tvs", "arguments": {"screen_size_inches": 55, "max_price": 50000}}}
    store = ConversationStore()
    guard_context(store, "1", "c", CHEAPER[0], 0)
    response = handle_message(served.tools, message, "s1", "2", True, lambda: guard_context(store, "2", "c", CHEAPER[1], 1))
    payload = json.loads(response["result"]["content"][0]["text"])
    assert response["result"]["isError"] is False and rejected(payload) == [("max_price", 50000)] and served.seen == []


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
    assert "never implied by a 'no'" in COUNT_SCOPE_NOTE and "`values` of the same entry" in COUNT_SCOPE_NOTE
    assert "`same_value_for_all`" in COUNT_SCOPE_NOTE and "attribute_values" not in with_features


# ---- the prompt is not part of the fix ------------------------------------------------------------------------

def test_the_system_prompt_is_the_accepted_v3_unchanged():
    """Phase 4F.3 measured a prompt v4 and did not adopt it (docs/PHASE_4F_3_MVP_HARDENING.md): the fixes are in the
    evidence, the guard and the tool contract. The deployed prompt is byte-identical to the one Phase 4F.2 ran."""
    run = json.loads((PROMPT_FILE.parents[2] / "evaluation/results/phase_4f_2/run_manifest.json").read_text(encoding="utf-8"))
    assert PROMPT_FILE.name == "agent_system_v3.md"
    assert hashlib.sha256(PROMPT_FILE.read_bytes()).hexdigest() == run["hashes"]["consultant/prompts/agent_system_v3.md"]
    assert not (PROMPT_FILE.parent / "agent_system_v4.md").exists()
