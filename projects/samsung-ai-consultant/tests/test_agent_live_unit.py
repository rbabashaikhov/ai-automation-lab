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


def test_bakeoff_driver_changes_only_the_model_id():
    committed = json.loads(open("workflows/ai-consultant.json", encoding="utf-8").read())
    for model in ("gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1"):
        wf = build_driver("followup-oled65-spike", "s", model)
        inlines = [json.loads(n["parameters"]["workflowJson"]) for n in wf["nodes"] if n["type"] == "n8n-nodes-base.executeWorkflow"]
        assert len(inlines) == 3
        for inline in inlines:
            changed = [a for a, b in zip(inline["nodes"], committed["nodes"]) if a != b]
            assert [n["name"] for n in changed] == ([] if model == "gpt-4.1-mini" else ["OpenAI Chat Model"])
            node = next(n for n in inline["nodes"] if n["name"] == "OpenAI Chat Model")
            assert node["parameters"] == {"model": {"__rl": True, "mode": "list", "value": model, "cachedResultName": model},
                                          "options": {"temperature": 0}}
            assert node["credentials"] == next(n for n in committed["nodes"] if n["name"] == "OpenAI Chat Model")["credentials"]
            assert inline["connections"] == committed["connections"] and inline["settings"] == committed["settings"]
    assert build_driver("no-tool-greeting", "s") == build_driver("no-tool-greeting", "s", "gpt-4.1-mini")   # default = committed


def test_bakeoff_plan_is_fixed_and_the_same_for_every_model():
    from evaluation import model_bakeoff as mb
    frozen = [c["id"] for c in load_cases()]
    supplement = load_cases(mb.SUPPLEMENT_CASES)                         # validated at the real tool boundary
    stage1 = mb.plan("stage1")
    assert [c for s, _, c in stage1 if s == "frozen"] == list(mb.STAGE1_FROZEN) and set(mb.STAGE1_FROZEN) <= set(frozen)
    assert [c for s, _, c in stage1 if s == "supplement"] == [c["id"] for c in supplement]
    assert [c for _, _, c in mb.plan("full")] == frozen and len(frozen) == 42
    assert not set(frozen) & {c["id"] for c in supplement}
    wf = build_driver("bake-mt-budget-override", "s", "gpt-4.1", mb.SUPPLEMENT_CASES)
    inputs = [json.loads(n["parameters"]["jsonOutput"])["chatInput"] for n in wf["nodes"] if n["name"].endswith("input")]
    assert inputs == ["Посоветуй телевизор до 120 тысяч.", "Можно до 160 тысяч.", "Цена уже не важна."]
    assert mb.MODELS[0] == mb.BASELINE == "gpt-4.1-mini" and set(mb.MODELS) == set(mb.MODEL_PRICES)


def test_bakeoff_turn_gap_only_adds_in_process_waits():
    plain = build_driver("followup-oled65-spike", "s", "gpt-4.1")
    paced = build_driver("followup-oled65-spike", "s", "gpt-4.1", None, 60)
    waits = [n for n in paced["nodes"] if n["type"] == "n8n-nodes-base.wait"]
    assert [n["name"] for n in waits] == ["Gap before turn 2", "Gap before turn 3"]
    assert all(n["parameters"] == {"amount": 60, "unit": "seconds"} for n in waits)      # < 65 s: n8n waits in memory
    assert [n for n in paced["nodes"] if n not in waits] == plain["nodes"]               # what the Agent receives is unchanged
    assert paced["connections"]["Turn 1"]["main"][0][0]["node"] == "Gap before turn 2"
    assert paced["connections"]["Gap before turn 2"]["main"][0][0]["node"] == "Turn 2 input"
    assert build_driver("no-tool-greeting", "s", None, None, 60) == build_driver("no-tool-greeting", "s")   # single turn: no wait


def test_bakeoff_report_reads_guard_decisions_and_final_arguments():
    from evaluation.bakeoff_report import final_arguments, guard_decisions
    log = "\n".join([
        '2026-10-01 06:01:25,877 consultant.mcp_server "POST /mcp?turn=941&conv=bake-x&q=[redacted]&h=0 HTTP/1.1" 200 -',
        "2026-10-01 06:01:25,957 consultant.agent_tools tool_call {'tool': 'recommend_tvs', 'status': 'ok', 'products': 3, 'gaps': [], "
        "'ms': 75, 'contract': 'agent-result-v2', 'guard': 'modified', 'detail': None, 'actions': "
        "[['removed', 'max_price', 150000, 'unsupported_price_value'], ['removed', 'required_features', 'hdmi_2_1', 'required_feature_not_mentioned']]}",
        '2026-10-01 06:01:25,958 consultant.mcp_server "POST /mcp?turn=941&conv=bake-x&q=[redacted]&h=0 HTTP/1.1" 200 -',
        "2026-10-01 06:01:26,100 consultant.agent_tools tool_call {'tool': 'get_tv', 'status': 'tool_call_limit_reached', 'products': 0, "
        "'gaps': [], 'ms': 0, 'contract': 'agent-result-v2'}",
        '2026-10-01 06:01:26,101 consultant.mcp_server "POST /mcp?turn=942&conv=bake-y&q=[redacted]&h=1 HTTP/1.1" 200 -'])
    d = guard_decisions(log)
    assert list(d) == ["941", "942"] and d["942"][0]["status"] == "not_run"              # refused by the cap before the guard
    g = d["941"][0]
    assert g["status"] == "modified" and [a["argument"] for a in g["actions"]] == ["max_price", "required_features"]
    proposed = {"panel_technology": ["OLED"], "use_cases": ["gaming"], "required_features": ["hdmi_2_1"], "max_price": 150000}
    assert final_arguments(proposed, g) == {"panel_technology": ["OLED"], "use_cases": ["gaming"]}
    assert proposed["max_price"] == 150000                                              # the proposal is not mutated
    report_only = {"status": "report_only", "actions": [{"action": "would_remove", "argument": "max_price", "value": 120000, "reason": "x"}]}
    assert final_arguments({"max_price": 120000}, report_only) == {"max_price": 120000}
    assert final_arguments({"max_price": 1000000}, {"status": "modified", "actions": [
        {"action": "removed", "argument": "max_price", "value": 1000000, "reason": "x"}]}) == {}
