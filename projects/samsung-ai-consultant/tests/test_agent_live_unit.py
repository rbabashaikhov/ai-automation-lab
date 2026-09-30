"""Gate 4D.2C live-evaluation tooling (offline: no n8n, no OpenAI)."""

import json

from evaluation.agent_eval import load_cases
from evaluation.agent_live import arg_taxonomy, build_driver, claim_check


def test_driver_runs_every_turn_of_one_case_in_one_session():
    wf = build_driver("followup-oled65-spike", "d2c-x")
    names = [n["name"] for n in wf["nodes"]]
    assert names == ["Manual Trigger", "Turn 1 input", "Turn 1", "Turn 2 input", "Turn 2", "Turn 3 input", "Turn 3"]
    assert "active" not in wf and not [n for n in wf["nodes"] if "webhook" in n["type"].lower()]
    turns = next(c for c in load_cases() if c["id"] == "followup-oled65-spike")["turns"]
    inputs = [json.loads(n["parameters"]["jsonOutput"]) for n in wf["nodes"] if n["name"].endswith("input")]
    assert [i["chatInput"] for i in inputs] == [t["user"] for t in turns]
    assert {i["sessionId"] for i in inputs} == {"d2c-x"}                       # one conversation per case
    runs = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.executeWorkflow"]
    committed = json.loads(open("workflows/ai-consultant.json", encoding="utf-8").read())
    for n in runs:
        inline = json.loads(n["parameters"]["workflowJson"])
        assert n["parameters"]["source"] == "parameter" and inline["nodes"] == committed["nodes"]
    assert len(build_driver("no-tool-greeting", "s")["nodes"]) == 3


def test_claim_check_verifies_per_model_price_availability_and_features():
    result = {"products": [{"model_code": "QE65S85HAEXPY", "price_rub": 189990, "list_price_rub": 229990, "available": True,
                            "specs": {"refresh_rate_hz": 120}, "features": {"vrr": "yes", "allm": "not_listed"},
                            "catalog_specs": [{"name": "Технология FreeSync", "value": "FreeSync Premium"}]}]}
    ok = "QE65S85HAEXPY — 189 990 ₽ (было 229 990 ₽), в наличии, 120 Гц, VRR, FreeSync Premium."
    assert claim_check(ok, [result]) == (6, [])
    bad = "QE65S85HAEXPY — 179 990 ₽, поддерживает ALLM."
    checked, issues = claim_check(bad, [result])
    assert issues == ["QE65S85HAEXPY: price 179990 not in evidence", "QE65S85HAEXPY: allm claimed, evidence state=not_listed"]
    assert claim_check("QE65S85HAEXPY: про ALLM в каталоге нет данных.", [result])[1] == []   # hedged: not a claim
    assert claim_check("QE55S95HAUXPY — 219 990 ₽", [result])[1] == [
        "QE55S95HAUXPY: named on a line but not in this session's evidence"]


def test_arg_taxonomy_separates_missing_and_invented_arguments():
    spec = {"tool": "recommend_tvs", "args": {"panel_technology": ["OLED"], "screen_size_inches": 65, "max_price": 200000,
                                             "use_cases": ["gaming"]}}
    calls = [{"tool": "recommend_tvs", "args": {"panel_technology": ["OLED"], "max_price": 200000, "use_cases": ["gaming", "movies"],
                                                "required_features": ["hdmi_2_1"], "min_price": 100000}}]
    t = arg_taxonomy(spec, calls, "Хочу OLED 65 дюймов до 200 тысяч для PS5.")
    assert t["missing_explicit"] == ["screen_size_inches"]
    assert t["invented"]["budget"] == ["min_price=100000 not stated by the user"]
    assert t["invented"]["required_features"] == ["required_features:hdmi_2_1 not named by the user"]
    assert t["invented"]["extra_use_cases"] == ["movies"] and t["invented_total"] == 3
