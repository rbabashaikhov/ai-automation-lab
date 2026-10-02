"""Phase 4F.3 MVP demo gate: the scenario file, the supporting checks (validated on the recorded Phase 4F.2 and 4F.3
evidence), the mechanical gate decision, and the committed results. Offline; no DB, n8n or LLM."""
import json
import re

import pytest

from evaluation import acceptance as acc
from evaluation import acceptance_report as rep
from evaluation import mvp_demo as m


@pytest.fixture(scope="module")
def data():
    return m.load()


@pytest.fixture(scope="module")
def results():
    return json.loads((m.OUT / "results.json").read_text(encoding="utf-8"))


def _turns(path):
    """``(scenario id, turn number, turn record, the conversation's tool results so far, the user messages so far)``."""
    for s in json.loads(path.read_text(encoding="utf-8"))["scenarios"]:
        session, users = [], []
        for t in s["turns"]:
            users.append(t["user"])
            session.extend(c["result"] for c in t["tool_calls"])
            yield s["scenario_id"], t["turn"], t, list(session), list(users)


# ---- scenarios ------------------------------------------------------------------------------------------

def test_scenario_file_is_valid_and_covers_the_brief(data):
    assert m.suite_ids(data, "targeted") == ["PA-02", "PA-04", "PA-08", "PA-15"]
    demo = m.suite_ids(data, "demo")
    assert demo == [f"DEMO-{i:02d}" for i in range(1, 9)] and 6 <= len(demo) <= 8
    assert sum(len(c["turns"]) for c in data["cases"]) == 22 and all(len(c["turns"]) >= 2 for c in data["cases"])
    titles = [s["title"] for s in data["scenarios"] if s["suite"] == "demo"]
    assert titles == ["Basic recommendation", "Gaming / PS5", "Movies + sound", "Follow-up: cheaper", "Change a hard constraint",
                      "Direct product comparison", "Missing attribute", "Aggregate / catalog question"]
    for s in data["scenarios"]:
        assert all(lim is None or set(lim) <= set(m.LIMIT_KEYS) for lim in s["limits"])
        if s["suite"] == "targeted":
            assert s["mvp_target"] in ("MVP-1", "MVP-2", "MVP-3") and s["old_failure"] and s["criterion"]
        else:
            assert s["shows"] and [st["after_turn"] for st in s["constraint_state"]] == list(range(1, len(s["limits"]) + 1))


def test_targeted_scenarios_are_the_unchanged_acceptance_turns(data):
    accepted = {c["id"]: [t["user"] for t in c["turns"]] for c in acc.load()["cases"]}
    for s in data["scenarios"]:
        if s["suite"] == "targeted":
            assert [t["user"] for t in m.turns_of(data, s)] == accepted[s["scenario_id"]]
    # the demo suite is not the acceptance benchmark: no demo turn is copied from an acceptance scenario
    demo_users = {t["user"] for c in data["cases"] for t in c["turns"]}
    assert len(demo_users) == 22 and not demo_users & {u for users in accepted.values() for u in users}


# ---- supporting checks, on recorded evidence -----------------------------------------------------------------

def test_unsupported_feature_check_flags_exactly_the_recorded_hdmi_claims():
    """Phase 4F.2: HDMI 2.1 attributed to the recommended TVs in PA-02 turn 1 (both runs) and PA-04 turn 1."""
    flagged = set()
    for label in ("original", "confirmation"):
        for sid, n, t, session, _ in _turns(rep.OUT / f"evidence_{label}.json"):
            issues = m.unsupported_feature_claims(t["answer"], session)
            if issues:
                assert {i["feature"] for i in issues} == {"hdmi_2_1"}
                flagged.add((label, sid, n))
    assert flagged == {("original", "PA-02", 1), ("original", "PA-04", 1), ("confirmation", "PA-02", 1)}


def test_unsupported_feature_check_reads_states_not_wording():
    session = [{"products": [{"model_code": "QE42S90HAEXPY", "specs": {"refresh_rate_hz": 120},
                              "features": {"vrr": "yes", "hdmi_2_1": "not_listed", "allm": "not_listed"}}]}]
    intro = "Рекомендую телевизоры с поддержкой HDMI 2.1 и VRR:\n1. Samsung 42\" OLED (QE42S90HAEXPY) — 120 Гц, VRR."
    assert [(i["feature"], i["not_yes_for"]) for i in m.unsupported_feature_claims(intro, session)] == [
        ("hdmi_2_1", {"QE42S90HAEXPY": "not_listed"})]                       # the introduction speaks for the listed model
    for ok in ("1. Samsung 42\" OLED (QE42S90HAEXPY) — 120 Гц, VRR.",
               "В каталоге нет данных о HDMI 2.1 для QE42S90HAEXPY.",
               "QE42S90HAEXPY: ALLM в каталоге не указан.",
               "Если HDMI 2.1 для вас обязателен, уточните, пожалуйста.",
               "HDMI 2.1 нужен консоли для 120 кадров в секунду."):             # general knowledge, no product named
        assert m.unsupported_feature_claims(ok, session) == [], ok
    assert m.unsupported_feature_claims("QE42S90HAEXPY поддерживает ALLM.", session)[0]["feature"] == "allm"


def test_numeric_argument_check_reads_slang_and_flags_the_recorded_ceiling():
    flagged = {(sid, n): [x for c in t["tool_calls"] for x in m.invented_numeric_arguments(c["final_args"], users)]
               for sid, n, t, _, users in _turns(rep.OUT / "evidence_original.json")}
    assert flagged[("PA-08", 3)] == ["max_price=40000 is stated in no user message",
                                     "min_screen_size_inches=40 is stated in no user message"]
    assert flagged[("PA-08", 2)] == []                                         # «43-50», «до сотки»: stated, slang included
    assert sum(bool(v) for v in flagged.values()) == 1
    assert m.invented_numeric_arguments({"max_price": 200000}, ["пара сотен максимум"]) == [
        "max_price=200000: not judged (a number word in the conversation could not be read)"]


def test_count_check_flags_the_recorded_dolby_atmos_count():
    flagged = {(sid, n): m.count_claims(t["answer"], session) for sid, n, t, session, _ in _turns(rep.OUT / "evidence_original.json")}
    assert [(i["count"], i["problem"]) for i in flagged[("PA-15", 3)]] == [(66, "no count of 66 for ['dolby_atmos'] in the evidence")]
    exact = [{"counts": {"available": 66}, "attribute_counts": {"dolby_atmos": {"available": {"yes": 54, "no": 0, "not_listed": 12}}}}]
    assert m.count_claims("Dolby Atmos указан у 54 моделей из 66 доступных.", exact) == []
    assert m.count_claims("В каталоге 66 доступных моделей с Dolby Atmos.", exact)[0]["count"] == 66
    assert m.count_claims("В каталоге 15 моделей OLED.", [{"counts": {"total": 15}}]) == []
    assert m.count_claims("Могу показать 3 варианта на 65 дюймов до 120 000 ₽.", [{"products": [{}, {}, {}]}]) == []


def test_value_check_flags_one_refresh_rate_stated_for_a_group():
    """Phase 4F.3 targeted run 1, PA-15 turn 2: «все … 60 Гц» from a count that only said "none is 120 Hz"."""
    run1 = {(sid, n): m.uniform_value_claims(t["answer"], [c["result"] for c in t["tool_calls"]], session)
            for sid, n, t, session, _ in _turns(m.OUT / "evidence_targeted_run1.json")}
    assert [(i["stated_hz"], i["evidence_hz"]) for i in run1[("PA-15", 2)]] == [(60, "no refresh-rate values in the evidence")]
    assert sum(bool(v) for v in run1.values()) == 1
    values = [{"attribute_counts": {"hz_120": {"total": {"yes": 0, "no": 10, "not_listed": 0}}},
               "attribute_values": {"hz_120": {"total": {"50": 2, "60": 8}}}}]
    assert m.uniform_value_claims("Все телевизоры до 50 тысяч работают на 60 Гц.", values, [])[0]["evidence_hz"] == [50, 60]
    assert m.uniform_value_claims("Все они имеют 50 или 60 Гц, моделей с 120 Гц нет.", values, []) == []
    oled = [{"attribute_counts": {"hz_120": {"total": {"yes": 15, "no": 0, "not_listed": 0}}}, "attribute_values": {"hz_120": {"total": {"120": 15}}}}]
    assert m.uniform_value_claims("У всех OLED в каталоге 120 Гц.", oled, []) == []
    nested = [{"attribute_counts": {"hz_120": {"total": {"yes": 0, "no": 10, "not_listed": 0, "values": {"50": 2, "60": 8},
                                                         "same_value_for_all": False}}}}]                 # the final contract
    assert m.listed_values(nested[0], "hz_120") == [{"50": 2, "60": 8}]
    assert m.uniform_value_claims("Все телевизоры до 50 тысяч работают на 60 Гц.", nested, [])[0]["evidence_hz"] == [50, 60]
    assert m.uniform_value_claims("Все они работают на 50 или 60 Гц.", nested, []) == []
    final = [m.uniform_value_claims(t["answer"], [c["result"] for c in t["tool_calls"]], session)
             for path in ("evidence_targeted.json", "evidence_demo.json") for _, _, t, session, _ in _turns(m.OUT / path)]
    assert not any(final)


def test_price_check_flags_a_price_that_belongs_to_another_product():
    """Phase 4F.3 targeted run 2: QE55QN80HAUXPY shown at 159 990 ₽, the price of QE65QN80HAUXPY."""
    run2 = {(sid, n): t["automated_checks"]["price_mismatches"] for sid, n, t, _, _ in _turns(m.OUT / "evidence_targeted_run2.json")}
    assert run2[("PA-02", 1)] == ["QE55QN80HAUXPY: price 159990 not in evidence"]
    assert sum(bool(v) for v in run2.values()) == 1
    final = {(sid, n): t["automated_checks"]["price_mismatches"] for path in ("evidence_targeted.json", "evidence_demo.json")
             for sid, n, t, _, _ in _turns(m.OUT / path) if t["automated_checks"]["price_mismatches"]}
    # PA-04 turn 2: the prices of the two other models are named on the S95H line (a comparison, not a wrong price).
    # DEMO-04 turn 2: «до 50 000 ₽» next to a model code -- the invented ceiling, reviewed as a blocking issue.
    assert final == {("PA-04", 2): ["QE65S95HAUXPY: price 289990 not in evidence", "QE65S95HAUXPY: price 189990 not in evidence"],
                     ("DEMO-04", 2): ["UE55M70HAUXPY: price 50000 not in evidence"]}


def test_unapplied_limit_check_flags_a_removed_limit_that_the_answer_still_states():
    """Phase 4F.3 final runs: the guard removed the invented limit, the answer worded it anyway (PA-08 turn 1, DEMO-04 turn 2)."""
    flagged = {(sid, n): [(i["argument"], i["value"]) for i in t["automated_checks"]["unapplied_limits_stated"]]
               for path in ("evidence_targeted.json", "evidence_demo.json") for sid, n, t, _, _ in _turns(m.OUT / path)
               if t["automated_checks"]["unapplied_limits_stated"]}
    assert flagged == {("PA-08", 1): [("max_screen_size_inches", 55)], ("DEMO-04", 2): [("max_price", 50000)]}
    call = lambda arg, v: [{"result": {"request": {"not_applied": {"constraints": [{"argument": arg, "value": v}]}}}}]  # noqa: E731
    stated = m.unapplied_limits_stated
    assert stated("Нет телевизоров с ценой до 50 000 ₽.", call("max_price", 50000))[0]["stated_as"] == "до 50 000"
    assert stated("Вариантов дешевле 50 тыс. нет.", call("max_price", 50000))[0]["stated_as"] == "дешевле 50 тыс"
    assert stated("Варианты с диагональю до 55 дюймов:", call("max_screen_size_inches", 55))[0]["value"] == 55
    assert stated("Самый доступный — за 52 490 ₽.", call("max_price", 50000)) == []                 # the limit is not worded
    assert stated("Samsung 55\" OLED за 150 000 ₽.", call("max_screen_size_inches", 55)) == []       # a product's own size
    assert stated("Модели до 50 000 ₽.", call("required_features", "hdmi_2_1")) == []               # only numeric limits
    assert stated("Модели до 50 000 ₽.", [{"result": {"request": {"constraints": ["price <= 50000"]}}}]) == []   # applied, stated by the user
    # Phase 4F.3A: the same check for a number that was rejected instead of removed
    rejected = lambda arg, v: [{"result": {"status": "invalid_arguments", "unsupported_numeric_constraints": [{"argument": arg, "value": v}]}}]  # noqa: E731
    assert stated("Нет телевизоров с ценой до 50 000 ₽.", rejected("max_price", 50000))[0]["stated_as"] == "до 50 000"
    assert stated("Варианты до 55 дюймов:", rejected("max_screen_size_inches", 55))[0]["argument"] == "max_screen_size_inches"
    assert stated("Самый доступный — за 52 490 ₽.", rejected("max_price", 50000)) == []


def test_limit_check_uses_the_evidence_facts():
    session = [{"products": [{"model_code": "QE65S95HAUXPY", "current_price_rub": 329990, "available": True,
                              "specs": {"screen_size_inches": 65, "panel_technology": "OLED"}},
                             {"model_code": "QE65S90HAEXPY", "current_price_rub": 289990, "available": True,
                              "specs": {"screen_size_inches": 65, "panel_technology": "OLED"}}]}]
    limits = {"panel": ["OLED"], "size": 65, "max_price": 300000, "available": True}
    assert m.products_outside_limits("QE65S90HAEXPY — 289 990 ₽; QE65S95HAUXPY — 329 990 ₽", session, limits) == [
        {"model_code": "QE65S95HAUXPY", "outside": ["price 329990"]}]
    assert m.products_outside_limits("QE65S90HAEXPY — 289 990 ₽", session, limits) == []
    assert m.products_outside_limits("QE65S95HAUXPY", session, None) == []


# ---- gate decision ------------------------------------------------------------------------------------------

def _scenario(sid, suite, issues=(), errors=(), defect_gone=True):
    turn = {"turn": 1, "issues": [{"kind": k, "text": "x"} for k in issues], "automated_checks": {"runtime": {"errors": list(errors)}}}
    blocking = any(k != m.NOTED for k in issues)
    return {"scenario_id": sid, "suite": suite, "turns": [turn], "verdict": "FAIL" if blocking else "PASS",
            **({"mvp_defect_gone": defect_gone} if suite == "targeted" else {})}


MANIFEST = {"run": {"unresolved_infrastructure_failures": []}}


@pytest.mark.parametrize("targeted_issue, demo_issues, errors, ready", [
    ({}, {}, {}, True),
    ({}, {"DEMO-03": ["noted"], "DEMO-07": ["noted", "noted"]}, {}, True),                 # noted issues never block
    ({"PA-02": ["unsupported_claim"]}, {}, {}, False),                                     # a blocking issue in a targeted turn
    ({"PA-08": ["invented_numeric_constraint"]}, {}, {}, False),                           # ... also when its own defect is gone
    ({}, {"DEMO-01": ["invented_price"]}, {}, False),
    ({}, {"DEMO-04": ["invented_numeric_constraint"]}, {}, False),
    ({}, {"DEMO-08": ["incorrect_aggregate"]}, {}, False),
    ({}, {"DEMO-05": ["constraint_change_failed"]}, {}, False),
    ({}, {"DEMO-02": ["hard_constraint_violation"]}, {}, False),
    ({}, {}, {"DEMO-06": ["error"]}, False),                                               # a runtime error
])
def test_gate_decision_on_synthetic_runs(targeted_issue, demo_issues, errors, ready):
    targeted = [_scenario(sid, "targeted", targeted_issue.get(sid, ())) for sid in ("PA-02", "PA-04", "PA-08", "PA-15")]
    demo = [_scenario(f"DEMO-{i:02d}", "demo", demo_issues.get(f"DEMO-{i:02d}", ()), errors.get(f"DEMO-{i:02d}", ())) for i in range(1, 9)]
    d = m.decide(targeted, demo, MANIFEST)
    assert d["ready"] is ready and d["final_line"] == m.FINAL_LINES[ready]


def test_gate_needs_six_passing_demo_conversations_and_all_four_targeted():
    targeted = [_scenario(sid, "targeted") for sid in ("PA-02", "PA-04", "PA-08", "PA-15")]
    assert m.decide(targeted, [_scenario(f"DEMO-{i:02d}", "demo") for i in range(1, 7)], MANIFEST)["ready"] is True
    assert m.decide(targeted, [_scenario(f"DEMO-{i:02d}", "demo") for i in range(1, 6)], MANIFEST)["ready"] is False
    assert m.decide(targeted[:3], [_scenario(f"DEMO-{i:02d}", "demo") for i in range(1, 9)], MANIFEST)["ready"] is False
    unresolved = {"run": {"unresolved_infrastructure_failures": ["x"]}}
    assert m.decide(targeted, [_scenario(f"DEMO-{i:02d}", "demo") for i in range(1, 9)], unresolved)["ready"] is False
    still_there = [_scenario(sid, "targeted", defect_gone=sid != "PA-15") for sid in ("PA-02", "PA-04", "PA-08", "PA-15")]
    d = m.decide(still_there, [_scenario(f"DEMO-{i:02d}", "demo") for i in range(1, 9)], MANIFEST)
    assert d["ready"] is False and [k for k, v in d["criteria"].items() if not v] == ["targeted_defects_gone"]


def test_review_must_cover_every_turn_and_state_the_targeted_defect(data):
    evidence = json.loads((m.OUT / "evidence_targeted.json").read_text(encoding="utf-8"))
    review = json.loads((m.OUT / "manual_review.json").read_text(encoding="utf-8"))["scenarios"]
    assert len(m.graded(data, evidence, review)) == 4
    for broken in ({**review, "PA-08": {**review["PA-08"], "turns": {k: v for k, v in review["PA-08"]["turns"].items() if k != "4"}}},
                   {**review, "PA-02": {**review["PA-02"], "mvp_defect_gone": "yes"}},
                   {**review, "PA-15": {**review["PA-15"], "turns": {**review["PA-15"]["turns"],
                                                                     "1": {"issues": [{"kind": "typo", "text": "x"}], "note": ""}}}}):
        with pytest.raises(m.ReviewError):
            m.graded(data, evidence, broken)


# ---- committed results --------------------------------------------------------------------------------------

def test_generated_files_are_current():
    assert m.stale_files() == []                      # results.json, the report and the transcripts follow from the inputs


def test_every_conversation_was_run_once_in_its_own_session_with_the_accepted_model(data, results):
    for suite in ("targeted", "demo"):
        assert [s["scenario_id"] for s in results[suite]] == m.suite_ids(data, suite)
        for s in results[suite]:
            spec = next(x for x in data["scenarios"] if x["scenario_id"] == s["scenario_id"])
            assert [t["user"] for t in s["turns"]] == [t["user"] for t in m.turns_of(data, spec)]       # exact wording and order
            for t in s["turns"]:
                assert t["answer"].strip() and t["model_as_sent"] == ["gpt-4.1-mini"] and t["temperature"] == 0
                assert t["prior_user_turns"] == t["turn"] - 1                                       # fresh session, memory carried
                assert t["consultant_saw"] is None or t["consultant_saw"]["conv"] == s["session_id"]
                assert not t["automated_checks"]["runtime"]["errors"]
    sessions = results["summary"]["sessions"]
    assert len(sessions) == len(set(sessions)) == 12 and results["summary"]["models_as_sent"] == ["gpt-4.1-mini"]


def test_decision_follows_from_the_review_and_is_stated_once(results):
    manifest = json.loads((m.OUT / "run_manifest.json").read_text(encoding="utf-8"))
    d = m.decide(results["targeted"], results["demo"], manifest)
    assert d == results["decision"] and d["final_line"] in m.FINAL_LINES.values()
    report = m.REPORT.read_text(encoding="utf-8")
    assert report.rstrip().endswith(f"**{d['final_line']}**")
    assert d["ready"] is all(d["criteria"].values())
    for s in (*results["targeted"], *results["demo"]):
        assert (s["verdict"] == "FAIL") == any(i["kind"] != m.NOTED for t in s["turns"] for i in t["issues"])


def test_recorded_decision_is_a_hold_for_one_reason(results):
    """The three targeted 4F.2 defects are gone and seven demo conversations pass; the gate is held by one criterion:
    a numeric limit the user never stated is still worded in two answers."""
    d = results["decision"]
    assert d["ready"] is False and d["final_line"] == "4F.3 MVP DEMO HOLD — FURTHER HARDENING REQUIRED"
    assert [k for k, v in d["criteria"].items() if not v] == ["zero_invented_numeric_constraints"]
    assert [(i["scenario_id"], i["turn"], i["kind"]) for i in d["blocking_issues"]] == [
        ("PA-08", 1, "invented_numeric_constraint"), ("DEMO-04", 2, "invented_numeric_constraint")]
    assert all(s["mvp_defect_gone"] is True for s in results["targeted"])
    assert d["targeted"] == {"PA-02": "PASS", "PA-04": "PASS", "PA-08": "FAIL", "PA-15": "PASS"}
    assert d["demo_passed"] == 7 and d["demo_total"] == 8 and d["demo"]["DEMO-04"] == "FAIL" and not d["runtime_errors"]
    # every automated flag of a blocking kind in the final runs is covered by the review
    for s in (*results["targeted"], *results["demo"]):
        for t in s["turns"]:
            if t["automated_checks"]["unapplied_limits_stated"]:
                assert [i["kind"] for i in t["issues"] if i["kind"] != m.NOTED] == ["invented_numeric_constraint"]
            assert not t["automated_checks"]["unsupported_feature_claims"] and not t["automated_checks"]["count_claims"]
            assert not t["automated_checks"]["invented_models"] and not t["automated_checks"]["products_outside_limits"]
            assert not t["automated_checks"]["invented_numeric_arguments"]            # no invented number reached the Core


def test_measurements_are_consistent_and_carry_the_reported_rates(results):
    data = json.loads((m.OUT / "measurements.json").read_text(encoding="utf-8"))
    sets = {s["id"]: s for s in data["sets"]}
    assert [r["id"] for r in data["summary"]] == list(sets) and len(sets) == 18 and sum(s["sessions"] for s in sets.values()) == 306
    assert results["measurements"] == data["summary"]
    for s in sets.values():
        rows = s["rows"]
        assert len(rows) == s["sessions"] == len({r["session"] for r in rows}) and all(r["answer"].strip() for r in rows)
        assert s["counts"] == {k: sum(r["classification"] == k for r in rows) for k in s["counts"]}
        assert sum(s["counts"].values()) == len(rows)
    attributed = lambda sid: sum(v for k, v in sets[sid]["counts"].items() if k.startswith("attributed"))      # noqa: E731
    assert [attributed(x) for x in ("BASE", "E2", "X1", "FIN_PS5", "FIN_PA04")] == [20, 20, 0, 0, 0]      # prompt v4 alone: no effect
    assert [sets[x]["answers_with_price_mismatch"] for x in ("BASE", "PS5D", "FIN_PS5")] == [0, 6, 0]     # why v4 was not adopted
    assert [sets[x]["counts"].get("false_all_60", 0) for x in ("FIN_HZ", "X2_HZ", "X3_HZ")] == [3, 3, 0]
    assert sets["FIN_ATMOS"]["counts"] == {"exact_counts": 10}
    voiced = [sets[x]["counts"].get("invented_limit_voiced", 0) for x in ("B_PA08T1", "F_PA08T1", "X4_PA08T1", "F_CHEAPER")]
    assert voiced == [11, 15, 14, 12] and sets["F_CHEAPER"]["sessions"] == 12
    for sid in ("B_PA08T1", "F_PA08T1", "X4_PA08T1", "F_CHEAPER"):                      # the guard removed it every time
        assert all(r["removed_by_guard"] for r in sets[sid]["rows"])


def test_transcripts_hold_the_full_conversations(results):
    report = m.REPORT.read_text(encoding="utf-8")
    for s in (*results["targeted"], *results["demo"]):
        path = m.OUT / "transcripts" / f"{s['scenario_id']}.md"
        text = path.read_text(encoding="utf-8")
        assert f"(transcripts/{path.name})" in report and f"session `{s['session_id']}`" in text and "`gpt-4.1-mini`" in text
        assert f"**Demo readiness: {s['verdict']}**" in text
        for t in s["turns"]:
            assert f"**User:** {t['user']}" in text and "Active constraints — hard:" in text
            assert all(f"> {line}" in text for line in t["answer"].splitlines() if line)
            assert all(f"`{c['tool']}`" in text and "arguments the Core received" in text for c in t["tool_calls"])
            assert all(i["text"] in text for i in t["issues"])


def test_phase_4f2_stays_on_hold_and_both_facts_are_stated():
    """Phase 4F.3 does not rewrite Phase 4F.2: its report, results and final line are untouched and still current."""
    assert rep.stale_files() == []
    assert rep.REPORT.read_text(encoding="utf-8").rstrip().endswith("**4F.2 HOLD — PRODUCT ACCEPTANCE FAILED**")
    report = m.REPORT.read_text(encoding="utf-8")
    assert "4F.2 HOLD — PRODUCT ACCEPTANCE FAILED" in report and "not accepted as production-ready" in report
    doc = (m.PROJECT / "docs/PHASE_4F_3_MVP_HARDENING.md").read_text(encoding="utf-8")
    assert "4F.2 HOLD — PRODUCT ACCEPTANCE FAILED" in doc


def test_no_secret_like_text_in_the_results():
    pattern = re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16}|sk-[A-Za-z0-9_-]{20}|X-N8N-API-KEY\W+\w{16}|postgres(ql)?://\S+:\S+@|"
                         r"CONSULTANT_MCP_TOKEN\s*=")
    for path in m.OUT.rglob("*"):
        if path.is_file():
            assert not pattern.search(path.read_text(encoding="utf-8")), path.name
