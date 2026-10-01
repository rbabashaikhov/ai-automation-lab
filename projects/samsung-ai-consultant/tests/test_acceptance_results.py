"""Phase 4F.2: the committed acceptance results are complete, consistent with the frozen rubric and reproducible
from the committed evidence and review. Offline; no DB, n8n or LLM."""
import json
import re

import pytest

from evaluation import acceptance as acc
from evaluation import acceptance_report as rep


@pytest.fixture(scope="module")
def dataset():
    return acc.load()


@pytest.fixture(scope="module")
def results():
    return json.loads((rep.OUT / "results.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest():
    return json.loads((rep.OUT / "run_manifest.json").read_text(encoding="utf-8"))


def test_generated_files_are_current():
    assert rep.stale_files() == []                 # results.json, the report and the 15 transcripts follow from the inputs


def test_every_scenario_and_turn_is_present_exactly_once(dataset, results):
    expected = {c["id"]: [t["user"] for t in c["turns"]] for c in dataset["cases"]}
    ids = [s["scenario_id"] for s in results["scenarios"]]
    assert ids == list(expected) and len(ids) == len(set(ids)) == 15
    for s in results["scenarios"]:
        assert [t["user"] for t in s["turns"]] == expected[s["scenario_id"]]            # exact wording, order and count
        assert [t["turn"] for t in s["turns"]] == list(range(1, len(s["turns"]) + 1))
    assert sum(len(s["turns"]) for s in results["scenarios"]) == 53
    assert results["summary"]["scenarios_executed"] == 15 and results["summary"]["user_turns_executed"] == 53


def test_every_turn_record_has_the_evidence_fields_of_the_design(dataset, results):
    fields = set(dataset["_meta"]["evidence_record_fields"])
    for run in ("scenarios", "confirmation"):
        for s in results[run]:
            for t in s["turns"]:
                assert fields <= set(t), (s["scenario_id"], t["turn"], sorted(fields - set(t)))
                assert t["answer"].strip() and t["model_as_sent"] == ["gpt-4.1-mini"] and t["temperature"] == 0


def test_every_failure_has_a_class_and_a_severity_from_the_frozen_taxonomy(dataset, results):
    classes = dataset["_meta"]["failure_classes"]
    for run in ("scenarios", "confirmation"):
        for s in results[run]:
            for t in s["turns"]:
                for i in t["issues"]:
                    assert i["class"] in classes and i["severity"] in rep.LEVELS and i["description"] and i["evidence"]
                above_minor = [i for i in t["issues"] if i["severity"] != "MINOR"]
                assert (t["verdict"] == "FAIL") == bool(above_minor)
            assert (s["verdict"] == "FAIL") == any(t["verdict"] == "FAIL" for t in s["turns"])


def test_sessions_are_isolated_and_memory_is_carried(results):
    sessions = [s["session_id"] for run in ("scenarios", "confirmation") for s in results[run]]
    assert len(sessions) == len(set(sessions)) == 26
    for run in ("scenarios", "confirmation"):
        for s in results[run]:
            for t in s["turns"]:
                assert t["prior_user_turns"] == t["turn"] - 1                       # 0 on the first turn of every scenario
                assert t["consultant_saw"] is None or t["consultant_saw"]["conv"] == s["session_id"]


def test_decision_follows_the_frozen_rule(dataset, results, manifest):
    review = json.loads((rep.OUT / "manual_review.json").read_text(encoding="utf-8"))
    d = rep.decide(dataset, results["scenarios"], results["confirmation"], manifest, review["scenario_assessment"])
    assert d == results["decision"]
    blockers = [i for run in ("scenarios", "confirmation") for s in results[run] for t in s["turns"] for i in t["issues"]
                if i["severity"] == "RELEASE_BLOCKER"]
    failed = [s for s in results["scenarios"] if s["verdict"] == "FAIL"]
    assert all(d["validity"].values())
    assert (d["outcome"] == "HOLD") == bool(blockers or len(failed) > 4 or len(d["systemic_major_classes"]) >= 2
                                            or any(x["all_failed"] for x in d["dimensions"].values()))
    assert {s["scenario_id"] for s in results["confirmation"]} == {s["scenario_id"] for s in failed}   # §4.2
    report = rep.REPORT.read_text(encoding="utf-8")
    assert report.rstrip().endswith(f"**{d['final_line']}**") and d["final_line"] == rep.FINAL_LINES[d["outcome"]]


def _scenario(dataset, sid, issues=()):
    """A synthetic graded scenario with the dataset's number of turns; the issues sit on its first turn."""
    n = len(next(c for c in dataset["cases"] if c["id"] == sid)["turns"])
    turns = [{"turn": k, "issues": [], "verdict": "PASS"} for k in range(1, n + 1)]
    turns[0]["issues"] = [{"class": c, "severity": s, "description": "x", "evidence": "y"} for c, s in issues]
    turns[0]["verdict"] = "FAIL" if any(s != "MINOR" for _, s in issues) else "PASS"
    dims = next(x for x in dataset["scenarios"] if x["scenario_id"] == sid)["dimensions"]
    return {"scenario_id": sid, "dimensions": dims, "turns": turns, "verdict": turns[0]["verdict"]}


@pytest.mark.parametrize("failures,confirm,assessment,expected", [
    ({}, {}, {}, "ACCEPT"),
    ({"PA-06": [("R2", "MINOR")]}, {}, {}, "ACCEPT"),
    ({"PA-06": [("R1", "MAJOR")]}, {"PA-06": []}, {"PA-06": {"cause": "c", "narrow_fix": None}}, "ACCEPT"),            # one-off
    ({"PA-06": [("R1", "MAJOR")], "PA-09": [("R1", "MAJOR")]}, {"PA-06": [], "PA-09": []},
     {"PA-06": {"cause": "c", "narrow_fix": True}, "PA-09": {"cause": "c", "narrow_fix": True}}, "CONDITIONAL ACCEPT"),  # systemic
    ({"PA-06": [("R1", "MAJOR")], "PA-09": [("R1", "MAJOR")]}, {"PA-06": [], "PA-09": []},
     {"PA-06": {"cause": "c", "narrow_fix": False}, "PA-09": {"cause": "c", "narrow_fix": True}}, "HOLD"),              # not narrow
    ({"PA-06": [("G4", "RELEASE_BLOCKER")]}, {"PA-06": []}, {"PA-06": {"cause": "c", "narrow_fix": True}}, "HOLD"),
    ({"PA-06": [("R1", "MAJOR")]}, {"PA-06": [("G1", "RELEASE_BLOCKER")]}, {"PA-06": {"cause": "c", "narrow_fix": True}}, "HOLD"),
    ({"PA-14": [("T1", "MAJOR")], "PA-15": [("K1", "MAJOR")]}, {"PA-14": [], "PA-15": []},
     {"PA-14": {"cause": "c", "narrow_fix": True}, "PA-15": {"cause": "c", "narrow_fix": True}}, "HOLD"),               # D10 all failed
    ({"PA-06": [("R1", "MAJOR")]}, {}, {"PA-06": {"cause": "c", "narrow_fix": True}}, "NO DECISION"),                   # no confirmation run
])
def test_decision_rule_on_synthetic_runs(dataset, manifest, failures, confirm, assessment, expected):
    ids = [c["id"] for c in dataset["cases"]]
    original = [_scenario(dataset, sid, failures.get(sid, ())) for sid in ids]
    confirmation = [_scenario(dataset, sid, issues) for sid, issues in confirm.items()]
    assert rep.decide(dataset, original, confirmation, manifest, assessment)["outcome"] == expected


def test_an_invalid_run_gives_no_decision(dataset, manifest):
    original = [_scenario(dataset, c["id"]) for c in dataset["cases"]]
    assert rep.decide(dataset, original, [], manifest, {})["outcome"] == "ACCEPT"
    drift = json.loads(json.dumps(manifest))
    drift["preflight"]["frozen_state_match"] = False
    assert rep.decide(dataset, original, [], drift, {})["outcome"] == "NO DECISION"
    assert rep.decide(dataset, original[:-1], [], manifest, {})["outcome"] == "NO DECISION"


def test_transcripts_hold_the_full_conversations(results):
    confirmed = {s["scenario_id"]: s for s in results["confirmation"]}
    report = rep.REPORT.read_text(encoding="utf-8")
    for s in results["scenarios"]:
        path = rep.OUT / "transcripts" / f"{s['scenario_id']}.md"
        text = path.read_text(encoding="utf-8")
        assert f"(transcripts/{path.name})" in report
        for run in [s] + ([confirmed[s["scenario_id"]]] if s["scenario_id"] in confirmed else []):
            for t in run["turns"]:
                assert f"**User:** {t['user']}" in text
                assert all(f"> {line}" in text for line in t["answer"].splitlines() if line)
                assert all(f"`{c['tool']}`" in text for c in t["tool_calls"])
                assert all(i["description"] in text for i in t["issues"])
        assert f"**Scenario verdict: {s['verdict']}**" in text


def test_the_rubric_and_the_runtime_sources_are_the_ones_that_were_run(results, manifest):
    assert results["_meta"]["dataset"]["sha256"] == acc.sha256(acc.DATASET) == manifest["hashes"]["evaluation/acceptance_scenarios.json"]
    for name, digest in acc.manifest()["frozen_files"].items():
        assert manifest["hashes"][name] == digest, name                             # no runtime source change since the run
    runner = manifest["run"]["runner"]["evaluation/acceptance_run.py"]
    assert acc.sha256(acc.PROJECT / "evaluation/acceptance_run.py") == runner
    assert acc.stale_blocks(acc.load()) == []                                       # the design document is still in sync


def test_no_secret_like_text_in_the_results():
    pattern = re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16}|sk-[A-Za-z0-9_-]{20}|X-N8N-API-KEY\W+\w{16}|postgres(ql)?://\S+:\S+@|"
                         r"CONSULTANT_MCP_TOKEN\s*=")
    for path in rep.OUT.rglob("*"):
        if path.is_file():
            assert not pattern.search(path.read_text(encoding="utf-8")), path.name
