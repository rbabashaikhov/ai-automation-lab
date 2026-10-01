"""Phase 4F.1: the product acceptance suite is well-formed, covers what the brief requires, is drivable by the
unchanged live tooling, and the design document restates it without drift. Offline; no DB, n8n or LLM."""
import copy
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from evaluation import acceptance as acc
from evaluation.agent_eval import load_cases
from evaluation.agent_live import build_driver


@pytest.fixture(scope="module")
def data():
    return acc.load()


def test_dataset_is_valid(data):
    acc.validate(data)


def test_scenario_ids_are_unique_and_counts_match_the_brief(data):
    ids = [s["scenario_id"] for s in data["scenarios"]]
    assert len(ids) == len(set(ids)) == 15 and 12 <= len(ids) <= 15
    primary = Counter(acc.primary_category(s) for s in data["scenarios"])
    assert primary == {"A": 3, "B": 4, "C": 2, "D": 2, "E": 1, "F": 1, "G": 2}
    cov = acc.coverage(data)
    assert cov["turns"] == 53 and cov["multi_turn_scenarios"] == 15 and cov["scenarios_with_3_to_5_turns"] == 15


def test_every_required_dimension_and_known_failure_mode_is_covered(data):
    cov = acc.coverage(data)
    assert list(cov["dimensions"]) == [f"D{i}" for i in range(1, 11)]
    assert all(len(sids) >= 2 for sids in cov["dimensions"].values())
    assert all(cov["historical_failure_modes"].values())


def test_every_scenario_has_the_fields_the_brief_requires(data):
    turns = {c["id"]: c["turns"] for c in data["cases"]}
    for s in data["scenarios"]:
        assert all(s[f] for f in acc.REQUIRED_FIELDS), s["scenario_id"]
        assert turns[s["scenario_id"]] and all(t["user"].strip() for t in turns[s["scenario_id"]])
        assert {f["severity"] for f in s["forbidden_behavior"]} <= set(data["_meta"]["severity_levels"])


def test_grounding_and_recommendation_failures_are_separate_classes(data):
    groups = {cid: c["group"] for cid, c in data["_meta"]["failure_classes"].items()}
    assert {"grounding", "constraint", "memory", "abstention", "recommendation", "safety", "style"} <= set(groups.values())
    assert all(c["default_severity"] == "MINOR" for c in data["_meta"]["failure_classes"].values() if c["group"] == "style")
    used = {f["class"] for s in data["scenarios"] for f in s["forbidden_behavior"]}
    assert {groups[c] for c in used} >= {"grounding", "constraint", "memory", "abstention", "recommendation"}
    # every scenario can fail on trust and not only on style
    assert all(any(f["severity"] != "MINOR" for f in s["forbidden_behavior"]) for s in data["scenarios"])


def test_cases_are_drivable_by_the_unchanged_live_tooling(data):
    cases = load_cases(acc.DATASET)                       # the closed-key validation agent_live.analyze applies
    assert [c["id"] for c in cases] == [s["scenario_id"] for s in data["scenarios"]]
    driver = build_driver("PA-10", "pa-test", cases_path=acc.DATASET)
    inputs = [json.loads(n["parameters"]["jsonOutput"]) for n in driver["nodes"] if n["name"].endswith("input")]
    assert [i["chatInput"] for i in inputs] == [t["user"] for t in cases[9]["turns"]]
    assert {i["sessionId"] for i in inputs} == {"pa-test"}                       # one session: memory within the scenario


def test_no_user_turn_is_copied_from_an_earlier_benchmark(data):
    users = {t["user"] for c in data["cases"] for t in c["turns"]}
    assert len(users) == 53 and not users & acc.earlier_texts()
    assert "Посоветуй телевизор для PS5." in acc.earlier_texts()                 # the comparison set is not empty


def test_document_restates_the_dataset_without_drift(data):
    assert acc.stale_blocks(data) == []
    text = acc.DOCUMENT.read_text(encoding="utf-8")
    assert acc.sha256(acc.DATASET) in text
    for s in data["scenarios"]:
        assert f"#### {s['scenario_id']} — " in text
    for case in data["cases"]:
        assert all(t["user"] in text for t in case["turns"])
    # the hand-written decision template uses the same denominators as the dataset
    for dim, sids in acc.coverage(data)["dimensions"].items():
        assert re.search(rf"\b{dim} __/{len(sids)}\b", text), dim
    assert "Scenarios executed:          __ / 15      Turns: __ / 53" in text
    for field in data["_meta"]["evidence_record_fields"]:                        # §8.1 lists list fields as `name[]`
        assert f"`{field}`" in text or f"`{field}[]`" in text, field


def test_document_records_the_frozen_file_hashes_of_the_acceptance_run():
    """§1.2 describes the product that Phase 4F.2 ran. Phase 4F.3 changed the product afterwards, so the section
    is compared with the 4F.2 run manifest, not with the working tree."""
    text = acc.DOCUMENT.read_text(encoding="utf-8")
    run = json.loads((acc.PROJECT / "evaluation/results/phase_4f_2/run_manifest.json").read_text(encoding="utf-8"))["hashes"]
    for name in ("consultant/prompts/agent_system_v3.md", "workflows/ai-consultant.json", "consultant/semantic_guard.py",
                 "consultant/agent_tools.py"):
        assert f"`{name}`, sha256 `{run[name][:16]}…`" in text, name
    assert acc.sha256(acc.PROJECT / "consultant/prompts/agent_system_v3.md") == run["consultant/prompts/agent_system_v3.md"]


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["scenarios"][1].update(scenario_id="PA-01"), "same ids"),
    (lambda d: d["scenarios"][0].pop("pass_criteria"), "missing or empty"),
    (lambda d: d["scenarios"][0].update(forbidden_behavior=[]), "missing or empty"),
    (lambda d: d["scenarios"][0]["forbidden_behavior"][0].update({"class": "Z9"}), "bad forbidden_behavior"),
    (lambda d: d["scenarios"][0]["forbidden_behavior"][0].update(severity="CRITICAL"), "bad forbidden_behavior"),
    (lambda d: d["scenarios"][0]["severity_if_failed"].update(worst_case="MINOR"), "severity_if_failed"),
    (lambda d: d["scenarios"][0]["constraint_state"].pop(), "one entry per turn"),
    (lambda d: d["scenarios"][0]["dimensions"].append("D11"), "bad dimensions"),
    (lambda d: d["cases"][0]["turns"][0].update(user=d["cases"][1]["turns"][0]["user"]), "duplicate user turn"),
    (lambda d: d["cases"][0]["turns"][0].update(user="Посоветуй телевизор для PS5."), "copied from an earlier"),
    (lambda d: d["cases"][0]["turns"][0].update(tool="run_sql"), "unknown tool"),
    (lambda d: [s["dimensions"].remove("D9") for s in d["scenarios"] if "D9" in s["dimensions"]], "fewer than 2"),
    (lambda d: [d[k].pop() for _ in range(4) for k in ("cases", "scenarios")], "11 scenarios"),
])
def test_validator_rejects_a_broken_dataset(data, mutate, match):
    broken = copy.deepcopy(data)
    mutate(broken)
    with pytest.raises(acc.AcceptanceError, match=match):
        acc.validate(broken)


def test_runtime_does_not_depend_on_the_acceptance_suite():
    runtime = Path(acc.PROJECT / "consultant")
    assert not [p.name for p in runtime.rglob("*.py") if "acceptance" in p.read_text(encoding="utf-8")]
