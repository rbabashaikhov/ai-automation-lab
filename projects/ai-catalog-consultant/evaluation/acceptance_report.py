"""Phase 4F.2 acceptance results: evidence collection, mechanical decision, transcripts and report.

    python -m evaluation.acceptance_report collect <run_dir> <consultant_log.txt> <catalog_codes.txt> <label>
    python -m evaluation.acceptance_report render          # results.json, transcripts/, report from committed inputs
    python -m evaluation.acceptance_report --check         # fail if a generated file is stale

``collect`` turns the raw traces of ``acceptance_run`` into ``evidence_<label>.json``: what was said and done per
turn (user turn, answer, tool calls with the arguments the model proposed, the guard's logged decision, the
arguments the Core received, the full tool result), the memory evidence, and the flags of the unchanged automated
layers. It needs the n8n API only for the start time of each execution. No model reasoning is recorded.

``render`` is offline. It joins the committed evidence with ``manual_review.json`` (the reviewer's issues per
turn) and ``run_manifest.json``, applies the frozen decision rule of docs/PHASE_4F_PRODUCT_ACCEPTANCE.md §4
mechanically, and writes ``results.json``, one transcript per scenario and the report. The rubric is not defined
here: classes, severities, dimensions and scenario tables come from ``acceptance_scenarios.json``.
"""
import json
import re
import sys
from pathlib import Path

from . import acceptance
from .agent_eval import MODEL_CODE, from_n8n_agent_output
from .agent_live import PROJECT, analyze, fetch, load_catalog_codes
from .bakeoff_report import final_arguments, guard_decisions

OUT = PROJECT / "evaluation/results/phase_4f_2"
REPORT = OUT / "PHASE_4F_2_PRODUCT_ACCEPTANCE_REPORT.md"
SET_NAME = "acceptance"
SECRET = re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16}|sk-[A-Za-z0-9_-]{20}|X-N8N-API-KEY\W+\w{16}|postgres(ql)?://\S+:\S+@")
REQUEST_H = re.compile(r'"POST /mcp\?turn=(\d+)&conv=([^&\s"]+)[^"\s]*?&h=(\d*)')
LEVELS = ("RELEASE_BLOCKER", "MAJOR", "MINOR")
ASPECTS = ("grounding", "hard_constraints", "memory_constraints", "recommendation_discipline", "abstention")
PRODUCT_URL = re.compile(r"/product/([A-Z0-9]+)/")


# ---- collect ---------------------------------------------------------------------------------------

def _evidence_products(results: list) -> dict:
    out = {}
    for r in results:
        for p in ((r or {}).get("products") or []) + ((r or {}).get("alternatives") or []):
            out.setdefault(p["model_code"], p)
    return out


def _product_facts(p: dict) -> dict:
    specs = p.get("specs") or {}
    return {"model_code": p["model_code"], "current_price_rub": p.get("current_price_rub"),
            **({"price_before_discount_rub": p["price_before_discount_rub"]} if p.get("price_before_discount_rub") else {}),
            "available": p.get("available"), "screen_size_inches": specs.get("screen_size_inches"),
            "panel_technology": specs.get("panel_technology"), "refresh_rate_hz": specs.get("refresh_rate_hz")}


def collect(run_dir: Path, log_file: Path, codes_file: Path, label: str) -> dict:
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text())
    log_text = Path(log_file).read_text()
    decisions = guard_decisions(log_text)
    h_seen = {m.group(1): {"conv": m.group(2), "h": m.group(3)} for m in REQUEST_H.finditer(log_text)}
    rows = {(r["case_id"], r["turn"]): r for r in analyze(run_dir / SET_NAME, load_catalog_codes(codes_file),
                                                         acceptance.DATASET)["turns"]}
    scenarios = []
    for sid in run["scenarios"]:
        trace = json.loads((run_dir / SET_NAME / f"{sid}.trace.json").read_text())
        turns, session_results = [], []
        for i, tt in enumerate(trace["turns"]):
            norm = from_n8n_agent_output(tt["user"], tt.get("agent_item") or {})
            guards = list(decisions.get(str(tt["sub_execution"]), []))
            calls = []
            for call in norm["tool_calls"]:
                proposed = {k: v for k, v in (call.get("args") or {}).items() if k != "tool"} \
                    if isinstance(call.get("args"), dict) else call.get("args")
                result = call.get("result") if isinstance(call.get("result"), dict) else {"status": None}
                # Parallel calls are logged in arrival order: match the guard decision by tool and outcome.
                g = next((x for x in guards if x["tool"] == call["tool"] and x["result_status"] == result.get("status")), None)
                if g is not None:
                    guards.remove(g)
                calls.append({"tool": call["tool"], "proposed_args": proposed,
                              "guard": {k: g[k] for k in ("status", "detail", "actions")} if g else {"status": "not_found_in_log"},
                              "final_args": final_arguments(proposed, g),
                              "result": {k: v for k, v in result.items() if k != "data_notice"}})
            session_results.extend(c["result"] for c in calls)
            known = _evidence_products(session_results)
            mentioned = sorted(set(MODEL_CODE.findall(norm["answer"])) | set(PRODUCT_URL.findall(norm["answer"])))
            row = rows[(sid, i)]
            sub = fetch(str(tt["sub_execution"]))
            options = (tt.get("model_options") or [{}])[0]
            turns.append({
                "turn": i + 1, "execution_id": str(tt["sub_execution"]), "timestamp_utc": sub.get("startedAt"),
                "model_as_sent": tt.get("models"), "temperature": options.get("temperature"),
                "max_tokens": options.get("max_tokens"), "user": tt["user"], "answer": norm["answer"],
                "prior_user_turns": tt.get("prior_user_turns"),
                "consultant_saw": h_seen.get(str(tt["sub_execution"])),       # conv and h as the Consultant logged them
                "tool_calls": calls,
                "products_mentioned": [_product_facts(known[c]) if c in known else {"model_code": c, "in_evidence": False}
                                       for c in mentioned],
                "latency_s": tt.get("latency_s"), "model_rounds": tt.get("model_rounds"), "tokens": tt.get("tokens"),
                "cost_usd": tt.get("cost_usd"),
                "automated_flags": {"auto_pass": row["auto_pass"], "tool_selection": row["selection"],
                                    "invented_arguments": {k: v for k, v in row["arg_taxonomy"]["invented"].items() if v},
                                    "hard_flags": row["grounding_hard_flags"], "soft_flags": row["grounding_soft_flags"],
                                    "claims_checked": row["claims_checked"], "claim_issues": row["claim_issues"],
                                    "leaks": row["leaks"], "blocked_by_cap": row["blocked_by_limit"],
                                    "clarification_asked": row["clarification_asked"]}})
        scenarios.append({"scenario_id": sid, "session_id": trace["session"], "started": trace["started"], "turns": turns})
    evidence = {"label": label, "run_id": run["run_id"], "started": run["started"], "finished": run["finished"],
                "model": run["model"], "infrastructure_events": run["infrastructure_events"], "scenarios": scenarios}
    text = json.dumps(evidence, ensure_ascii=False, indent=1)
    assert not SECRET.search(text), "secret-like text in the evidence"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"evidence_{label}.json").write_text(text + "\n", encoding="utf-8")
    return evidence


# ---- review + decision (offline) -------------------------------------------------------------------

ASPECT_GROUPS = {"grounding": ("grounding", "safety"), "hard_constraints": ("constraint",), "memory_constraints": ("memory",),
                 "recommendation_discipline": ("recommendation", "comparison", "boundary", "style"),
                 "abstention": ("abstention",)}
FINAL_LINES = {"ACCEPT": "4F.2 ACCEPT — READY FOR RELEASE DECISION",
               "CONDITIONAL ACCEPT": "4F.2 CONDITIONAL ACCEPT — TARGETED REMEDIATION REQUIRED",
               "HOLD": "4F.2 HOLD — PRODUCT ACCEPTANCE FAILED", "NO DECISION": "4F.2 NO DECISION — RUN INVALID"}


class ReviewError(ValueError):
    pass


def _load(name: str) -> dict:
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def _worst(severities) -> str:
    present = set(severities)
    return next((level for level in LEVELS if level in present), "NONE")


def graded_turn(dataset: dict, scenario: dict, state: dict, ev_scenario: dict, turn: dict, review: dict, run_id: str) -> dict:
    """One per-turn record with the fields of the design document §8.1."""
    classes = dataset["_meta"]["failure_classes"]
    issues = review.get("issues", [])
    for i in issues:
        if set(i) != {"class", "severity", "description", "evidence"} or i["class"] not in classes or i["severity"] not in LEVELS:
            raise ReviewError(f"{scenario['scenario_id']}[{turn['turn']}]: bad issue {i}")
    failing = [i for i in issues if i["severity"] != "MINOR"]
    aspects = {}
    for aspect, groups in ASPECT_GROUPS.items():
        own = [i for i in issues if classes[i["class"]]["group"] in groups]
        result = "FAIL" if any(i["severity"] != "MINOR" for i in own) else "N/A" if aspect in review.get("na", []) else "PASS"
        aspects[aspect] = {"result": result, "issues": [i["class"] for i in own]}
    c = review.get("constraints")
    if state["hard"] and c is None and "hard_constraints" not in review.get("na", []):
        raise ReviewError(f"{scenario['scenario_id']}[{turn['turn']}]: constraints not reviewed")
    violated = set((c or {}).get("violated", []))
    if violated - set(state["hard"]):
        raise ReviewError(f"{scenario['scenario_id']}[{turn['turn']}]: unknown violated constraint")
    nothing_presented = review.get("recommended") == []          # no product presented as matching: nothing to satisfy
    checks = [] if c is None else [{"constraint": h, "satisfied": None if nothing_presented else h not in violated,
                                    "evidence": c["evidence"]} for h in state["hard"]]
    if nothing_presented and aspects["hard_constraints"]["result"] == "PASS":
        aspects["hard_constraints"]["result"] = "N/A"
    mentioned = [p["model_code"] for p in turn["products_mentioned"]]
    return {
        "run_id": run_id, "scenario_id": scenario["scenario_id"], "turn": turn["turn"], "session_id": ev_scenario["session_id"],
        "execution_id": turn["execution_id"], "timestamp_utc": turn["timestamp_utc"], "model_as_sent": turn["model_as_sent"],
        "temperature": turn["temperature"], "user": turn["user"], "answer": turn["answer"],
        "prior_user_turns": turn["prior_user_turns"], "consultant_saw": turn["consultant_saw"], "tool_calls": turn["tool_calls"],
        "products_mentioned": turn["products_mentioned"],
        "products_recommended": review.get("recommended", mentioned),
        "active_constraints": {k: state[k] for k in ("hard", "soft", "replaced", "released", "note") if state.get(k)},
        "hard_constraints": {"result": aspects["hard_constraints"]["result"], "issues": aspects["hard_constraints"]["issues"],
                             "checks": checks},
        "grounding": aspects["grounding"], "memory_constraints": aspects["memory_constraints"],
        "recommendation_discipline": aspects["recommendation_discipline"], "abstention": aspects["abstention"],
        "automated_flags": turn["automated_flags"], "issues": issues, "notes": review.get("note", ""),
        "severity": _worst(i["severity"] for i in issues), "verdict": "FAIL" if failing else "PASS",
        "latency_s": turn["latency_s"], "tokens": turn["tokens"], "cost_usd": turn["cost_usd"], "model_rounds": turn["model_rounds"]}


def graded_run(dataset: dict, evidence: dict, reviews: dict) -> list:
    scenarios = {s["scenario_id"]: s for s in dataset["scenarios"]}
    cases = {c["id"]: c for c in dataset["cases"]}
    out = []
    for ev in evidence["scenarios"]:
        sid = ev["scenario_id"]
        s, review = scenarios[sid], reviews.get(sid)
        if review is None or set(review["turns"]) != {str(t["turn"]) for t in ev["turns"]}:
            raise ReviewError(f"{sid}: the review must cover every turn of the {evidence['label']} run")
        if [t["user"] for t in ev["turns"]] != [t["user"] for t in cases[sid]["turns"]]:
            raise ReviewError(f"{sid}: executed turns differ from the dataset")
        turns = [graded_turn(dataset, s, s["constraint_state"][t["turn"] - 1], ev, t, review["turns"][str(t["turn"])],
                             evidence["run_id"]) for t in ev["turns"]]
        issues = [i for t in turns for i in t["issues"]]
        out.append({"scenario_id": sid, "title": s["title"], "category": s["category"], "dimensions": s["dimensions"],
                    "session_id": ev["session_id"], "started": ev["started"],
                    "verdict": "FAIL" if any(t["verdict"] == "FAIL" for t in turns) else "PASS",
                    "severity": _worst(i["severity"] for i in issues),
                    "failing_turns": [t["turn"] for t in turns if t["verdict"] == "FAIL"],
                    "classes": sorted({i["class"] for i in issues if i["severity"] != "MINOR"}),
                    "minor_classes": sorted({i["class"] for i in issues if i["severity"] == "MINOR"}),
                    "note": review.get("note", ""), "turns": turns})
    return out


def decide(dataset: dict, original: list, confirmation: list, manifest: dict, assessment: dict) -> dict:
    """The decision rule of docs/PHASE_4F_PRODUCT_ACCEPTANCE.md §4, applied mechanically."""
    expected = [c["id"] for c in dataset["cases"]]
    turns_expected = sum(len(c["turns"]) for c in dataset["cases"])
    valid = {"frozen_state_match": bool(manifest["preflight"]["frozen_state_match"]),
             "all_scenarios_executed": [s["scenario_id"] for s in original] == expected,
             "all_turns_recorded": sum(len(s["turns"]) for s in original) == turns_expected,
             "no_unresolved_infrastructure_failure": not manifest["run"]["unresolved_infrastructure_failures"]}
    failed = [s["scenario_id"] for s in original if s["verdict"] == "FAIL"]
    confirmed = {s["scenario_id"]: s for s in confirmation}
    blockers = [{"run": label, "scenario_id": s["scenario_id"], "turn": t["turn"], "class": i["class"], "description": i["description"]}
                for label, run in (("original", original), ("confirmation", confirmation)) for s in run for t in s["turns"]
                for i in t["issues"] if i["severity"] == "RELEASE_BLOCKER"]
    majors = {}
    for s in original:
        for t in s["turns"]:
            for i in t["issues"]:
                if i["severity"] == "MAJOR":
                    majors.setdefault(i["class"], []).append((s["scenario_id"], t["turn"]))
    kinds = {}
    for cls, where in sorted(majors.items()):
        scen = sorted({sid for sid, _ in where})
        if len(scen) >= 2 or len(set(where)) >= 3:
            kind = "systemic"
        else:
            again = {(sid, t["turn"]) for sid in scen if sid in confirmed for t in confirmed[sid]["turns"]
                     for i in t["issues"] if i["class"] == cls and i["severity"] != "MINOR"}
            kind = "reproducible" if again & set(where) else "one-off"
        kinds[cls] = {"kind": kind, "scenarios": scen, "turns": [f"{sid}[{n}]" for sid, n in sorted(set(where))]}
    systemic = [c for c, k in kinds.items() if k["kind"] == "systemic"]
    dims = {}
    for d in dataset["_meta"]["dimensions"]:
        members = [s for s in original if d in s["dimensions"]]
        passed = sum(s["verdict"] == "PASS" for s in members)
        dims[d] = {"passed": passed, "total": len(members), "at_least_half": 2 * passed >= len(members),
                   "all_failed": bool(members) and passed == 0,
                   "failed": [s["scenario_id"] for s in members if s["verdict"] == "FAIL"]}
    missing_confirmation = [sid for sid in failed if sid not in confirmed]
    understood = all(assessment.get(sid, {}).get("cause") for sid in failed)
    narrow = all(assessment.get(sid, {}).get("narrow_fix") is True for sid in failed)       # None = not assessed
    not_narrow = [sid for sid in failed if assessment.get(sid, {}).get("narrow_fix") is False]
    checks = {"zero_release_blockers": not blockers, "no_systemic_major_class": not systemic,
              "at_most_one_systemic_major_class": len(systemic) <= 1, "failed_scenarios_at_most_2": len(failed) <= 2,
              "failed_scenarios_at_most_4": len(failed) <= 4,
              "every_dimension_at_least_half": all(d["at_least_half"] for d in dims.values()),
              "no_dimension_all_failed": not any(d["all_failed"] for d in dims.values()),
              "every_failure_cause_understood": understood,
              "every_fix_narrow": None if failed and not narrow and not not_narrow else narrow,       # None = not assessed
              "confirmation_run_for_every_failed_scenario": not missing_confirmation}
    hold_reasons = [text for cond, text in (
        (blockers, f"{len(blockers)} RELEASE_BLOCKER issue(s)"),
        (len(failed) > 4, f"{len(failed)} failed scenarios (more than 4)"),
        (len(systemic) >= 2, f"{len(systemic)} systemic MAJOR classes (two or more)"),
        (any(d["all_failed"] for d in dims.values()),
         "dimension(s) in which every scenario failed: " + ", ".join(k for k, d in dims.items() if d["all_failed"])),
        (not_narrow, "a fix that is not narrow: " + ", ".join(not_narrow))) if cond]
    if not all(valid.values()) or missing_confirmation:
        outcome = "NO DECISION"
    elif hold_reasons:
        outcome = "HOLD"
    elif all(checks[k] for k in ("zero_release_blockers", "no_systemic_major_class", "failed_scenarios_at_most_2",
                                 "every_dimension_at_least_half", "every_failure_cause_understood")):
        outcome = "ACCEPT"
    elif all(checks[k] for k in ("zero_release_blockers", "at_most_one_systemic_major_class", "failed_scenarios_at_most_4",
                                 "every_failure_cause_understood", "every_fix_narrow")):
        outcome = "CONDITIONAL ACCEPT"
    else:
        outcome = "HOLD"
    return {"outcome": outcome, "final_line": FINAL_LINES[outcome], "validity": valid, "rule_checks": checks,
            "hold_reasons": hold_reasons if outcome == "HOLD" else [], "failed_scenarios": failed,
            "release_blockers": blockers, "major_classes": kinds, "systemic_major_classes": systemic, "dimensions": dims}


def build() -> dict:
    dataset = acceptance.load()
    acceptance.validate(dataset)
    manifest, review = _load("run_manifest.json"), _load("manual_review.json")
    ev_original, ev_confirm = _load("evidence_original.json"), _load("evidence_confirmation.json")
    original = graded_run(dataset, ev_original, review["original"])
    confirmation = graded_run(dataset, ev_confirm, review["confirmation"])
    decision = decide(dataset, original, confirmation, manifest, review["scenario_assessment"])
    classes = dataset["_meta"]["failure_classes"]
    issues = [(s["scenario_id"], t["turn"], i) for s in original for t in s["turns"] for i in t["issues"]]
    by_level = {lv: sum(i["severity"] == lv for _, _, i in issues) for lv in LEVELS}
    by_class, by_group = {}, {}
    for _, _, i in issues:
        by_class.setdefault(i["class"], {lv: 0 for lv in LEVELS})[i["severity"]] += 1
        if i["severity"] != "MINOR":
            g = classes[i["class"]]["group"]
            by_group[g] = by_group.get(g, 0) + 1
    all_turns = [t for s in original for t in s["turns"]]
    lat = sorted(t["latency_s"] for t in all_turns)
    calls = [c for t in all_turns for c in t["tool_calls"]]
    guard = {}
    for c in calls:
        guard[c["guard"]["status"]] = guard.get(c["guard"]["status"], 0) + 1
    statuses = {}
    for c in calls:
        statuses[c["result"].get("status")] = statuses.get(c["result"].get("status"), 0) + 1
    summary = {
        "scenarios_executed": len(original), "scenarios_passed": sum(s["verdict"] == "PASS" for s in original),
        "scenarios_failed": len(decision["failed_scenarios"]), "user_turns_executed": len(all_turns),
        "turns_passed": sum(t["verdict"] == "PASS" for t in all_turns),
        "turns_failed": sum(t["verdict"] == "FAIL" for t in all_turns),
        "issues_by_severity": by_level, "issues_by_class": dict(sorted(by_class.items())),
        "issues_above_minor_by_group": dict(sorted(by_group.items())),
        "sessions": len({s["session_id"] for s in original}),
        "session_isolation": {"first_turn_prior_user_turns": sorted({s["turns"][0]["prior_user_turns"] for s in original}),
                              "memory_carried": all(t["prior_user_turns"] == t["turn"] - 1 for t in all_turns),
                              "consultant_saw_matching_session": all(t["consultant_saw"] is None or
                                                                     t["consultant_saw"]["conv"] == t["session_id"] for t in all_turns)},
        "models_as_sent": sorted({m for t in all_turns for m in (t["model_as_sent"] or [])}),
        "temperatures": sorted({t["temperature"] for t in all_turns}),
        "tool_calls": len(calls), "tool_result_statuses": statuses, "guard_decisions": guard,
        "max_tool_calls_per_turn": max(len(t["tool_calls"]) for t in all_turns),
        "execution_errors": 0 if all(t["answer"] for t in all_turns) else None,
        "infrastructure_events": {"original": ev_original["infrastructure_events"], "confirmation": ev_confirm["infrastructure_events"]},
        "confirmation_runs": {"scenarios": [s["scenario_id"] for s in confirmation],
                              "turns": sum(len(s["turns"]) for s in confirmation),
                              "passed": [s["scenario_id"] for s in confirmation if s["verdict"] == "PASS"],
                              "failed": [s["scenario_id"] for s in confirmation if s["verdict"] == "FAIL"]},
        "invalid_scenarios": [],
        "latency_s": {"median": lat[len(lat) // 2], "max": lat[-1]},
        "tokens_n8n_estimate": {"prompt": sum(t["tokens"]["promptTokens"] for t in all_turns),
                                "completion": sum(t["tokens"]["completionTokens"] for t in all_turns)},
        "cost_usd_n8n_estimate": {"original": round(sum(t["cost_usd"] for t in all_turns), 4),
                                  "confirmation": round(sum(t["cost_usd"] for s in confirmation for t in s["turns"]), 4)},
    }
    return {"_meta": {"phase": "4F.2", "rubric": "docs/PHASE_4F_PRODUCT_ACCEPTANCE.md",
                      "dataset": {"version": dataset["_meta"]["version"], "sha256": acceptance.sha256(acceptance.DATASET)},
                      "run_id": ev_original["run_id"], "confirmation_run_id": ev_confirm["run_id"],
                      "generated_by": "python -m evaluation.acceptance_report render",
                      "inputs": ["evidence_original.json", "evidence_confirmation.json", "manual_review.json", "run_manifest.json"],
                      "contents": "No model reasoning is recorded."},
            "decision": decision, "summary": summary, "scenarios": original, "confirmation": confirmation,
            "review": {k: review[k] for k in ("_meta", "scenario_assessment", "historical_failure_modes", "what_held",
                                                "design_issues", "observations", "truth_checks")}}


# ---- transcripts and report ------------------------------------------------------------------------

def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def _rub(n) -> str:
    return f"{n:,}".replace(",", " ") + " ₽" if isinstance(n, (int, float)) else "—"


def _product_line(p: dict) -> str:
    sp = p.get("specs") or {}
    bits = [f"`{p['model_code']}`", _rub(p.get("current_price_rub"))
            + (f" (без скидки {_rub(p['price_before_discount_rub'])})" if p.get("price_before_discount_rub") else ""),
            "available" if p.get("available") else "not available",
            f"{sp.get('screen_size_inches')}″ {sp.get('panel_technology')}, {sp.get('refresh_rate_hz')} Hz"]
    if p.get("features"):
        bits.append("features " + ", ".join(f"{k}={v if isinstance(v, str) else str(v.get('state')) + ':' + str(v.get('value'))}"
                                            for k, v in p["features"].items()))
    violated = [c["constraint"] for c in p.get("constraints") or [] if c.get("satisfied") is False]
    if violated:
        bits.append("violates " + "; ".join(violated))
    return "; ".join(bits)


def _tool_lines(call: dict, n: int) -> list:
    g, r = call["guard"], call["result"]
    acts = "; ".join(f"{a['action']} {a['argument']}={_j(a['value'])} ({a['reason']})" for a in g.get("actions", []))
    req = r.get("request") or {}
    out = [f"- Tool call {n}: `{call['tool']}`",
           f"  - arguments proposed by the model: `{_j(call['proposed_args'])}`",
           f"  - guard: **{g['status']}**" + (f" — {acts}" if acts else ""),
           f"  - arguments the Core received: `{_j(call['final_args'])}`",
           f"  - result: status `{r.get('status')}`, confidence `{r.get('confidence')}`, totals `{_j(r.get('totals'))}`"]
    for key in ("constraints", "use_cases", "required_features", "preferred_features", "attributes_asked", "question",
                "model_resolution", "not_applied"):
        if req.get(key):
            out.append(f"  - request.{key}: `{_j(req[key])}`")
    for key in ("confidence_notes", "counts", "groups", "comparison", "clarification", "errors"):
        if r.get(key):
            out.append(f"  - {key}: `{_j(r[key])}`")
    for gap in r.get("gaps") or []:
        out.append(f"  - gap `{gap.get('kind')}`: {gap.get('detail', '')}")
    for label in ("products", "alternatives"):
        for p in r.get(label) or []:
            out.append(f"  - {label[:-1]}: {_product_line(p)}")
            rows = "; ".join(f"{x['name']}: {x['value']}" for x in p.get("catalog_specs") or [])
            if rows:
                out.append(f"    - catalog rows: {rows}")
            for ps in p.get("catalog_passages") or []:
                out.append(f"    - catalog passage ({ps.get('section')}): " + " / ".join(ps.get("text", "").splitlines()))
    return out


def _turn_lines(t: dict) -> list:
    ac = t["active_constraints"]
    out = [f"### Turn {t['turn']} — {t['verdict']}" + (f" ({t['severity']})" if t["severity"] != "NONE" else ""), "",
           f"**User:** {t['user']}", "",
           f"- Execution `{t['execution_id']}`, {t['timestamp_utc']}; model as sent `{', '.join(t['model_as_sent'] or [])}`, "
           f"temperature {t['temperature']}; earlier user turns in the Agent's memory: {t['prior_user_turns']}; "
           f"latency {t['latency_s']} s",
           f"- Active constraints — hard: {'; '.join(ac.get('hard', [])) or 'none'}"
           + (f" · soft: {'; '.join(ac['soft'])}" if ac.get("soft") else "")
           + (f" · replaced: {'; '.join(ac['replaced'])}" if ac.get("replaced") else "")
           + (f" · released: {'; '.join(ac['released'])}" if ac.get("released") else "")
           + (f" · note: {ac['note']}" if ac.get("note") else "")]
    if not t["tool_calls"]:
        out.append("- Tool calls: none")
    for n, call in enumerate(t["tool_calls"], start=1):
        out += _tool_lines(call, n)
    out += ["", "**Consultant:**", ""] + [f"> {x}" if x else ">" for x in (t["answer"] or "(empty)").splitlines()]
    out += ["", "**Review**", "",
            f"- Grounding: {t['grounding']['result']} · hard constraints: {t['hard_constraints']['result']} · memory: "
            f"{t['memory_constraints']['result']} · recommendation discipline: {t['recommendation_discipline']['result']} · "
            f"abstention: {t['abstention']['result']}"]
    for c in t["hard_constraints"]["checks"]:
        state = "not applicable" if c["satisfied"] is None else "satisfied" if c["satisfied"] else "VIOLATED"
        out.append(f"- Constraint «{c['constraint']}»: {state} — {c['evidence']}")
    for i in t["issues"]:
        out.append(f"- **{i['class']} · {i['severity']}** — {i['description']} *Evidence:* {i['evidence']}")
    if t["notes"]:
        out.append(f"- Notes: {t['notes']}")
    flags = {k: v for k, v in t["automated_flags"].items() if v and k not in ("claims_checked", "tool_selection", "clarification_asked")}
    out += [f"- Automated flags (supporting only): `{_j(flags)}`", f"- **{t['verdict']}**", ""]
    return out


def render_transcript(scenario: dict, confirmation, dataset: dict) -> str:
    meta = dataset["_meta"]
    out = [f"# {scenario['scenario_id']} — {scenario['title']}", "",
           "Phase 4F.2 conversation transcript. Generated by `python -m evaluation.acceptance_report render` from the committed "
           "evidence and review files; not edited by hand. No model reasoning is recorded.", "",
           f"- Category: {', '.join(scenario['category'])} · dimensions: {' '.join(scenario['dimensions'])}",
           f"- Original run: session `{scenario['session_id']}`, started {scenario['started']}",
           f"- **Scenario verdict: {scenario['verdict']}**" + (f" — {scenario['severity']}; classes {', '.join(scenario['classes'])}; "
                                                              f"failing turns {', '.join(map(str, scenario['failing_turns']))}"
                                                              if scenario["verdict"] == "FAIL" else
                                                              (f" with notes ({', '.join(scenario['minor_classes'])})" if scenario["minor_classes"] else "")),
           f"- Reviewer note: {scenario['note']}",
           f"- Rubric: [scenario card](../../../../docs/PHASE_4F_PRODUCT_ACCEPTANCE.md) §6.4, failure classes §3.2 "
           f"({len(meta['failure_classes'])} classes)", "", "## Original run", ""]
    for t in scenario["turns"]:
        out += _turn_lines(t)
    if confirmation:
        out += ["## Confirmation run", "",
                f"Session `{confirmation['session_id']}`, started {confirmation['started']}. It classifies the failures of the original "
                f"run and does not change its verdict. Result: **{confirmation['verdict']}**"
                + (f" ({', '.join(confirmation['classes'])})" if confirmation["classes"] else "") + f". {confirmation['note']}", ""]
        for t in confirmation["turns"]:
            out += _turn_lines(t)
    return "\n".join(out).rstrip() + "\n"


def _table(header: list, rows: list) -> list:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]


def render_report(res: dict, manifest: dict, dataset: dict) -> str:
    d, s, review = res["decision"], res["summary"], res["review"]
    pre, run = manifest["preflight"], manifest["run"]
    classes, dims = dataset["_meta"]["failure_classes"], dataset["_meta"]["dimensions"]
    confirmed = {c["scenario_id"]: c for c in res["confirmation"]}
    out = ["# Phase 4F.2 — Product Acceptance Report", "",
           f"**{d['final_line']}**", "",
           "The frozen Samsung AI Consultant was run once through the 15 acceptance conversations of Phase 4F.1. Nothing in the "
           "evaluated system was changed before, during or after the run. This report is generated by "
           "`python -m evaluation.acceptance_report render` from the files in this directory; the rubric is "
           "[docs/PHASE_4F_PRODUCT_ACCEPTANCE.md](../../../docs/PHASE_4F_PRODUCT_ACCEPTANCE.md).", "",
           *_table(["File", "Contents"], [
               ["[run_manifest.json](run_manifest.json)", "preflight evidence, effective configuration, hashes, sessions, cleanup"],
               ["[evidence_original.json](evidence_original.json), [evidence_confirmation.json](evidence_confirmation.json)",
                "per turn: user turn, answer, tool calls (proposed arguments, guard decision, arguments the Core received, full tool result), memory evidence, automated flags"],
               ["[manual_review.json](manual_review.json)", "the reviewer's issues per turn, truth checks, design issues, observations"],
               ["[results.json](results.json)", "graded per-turn records (§8.1 fields), scenario verdicts, counts, the mechanical decision"],
               ["[transcripts/](transcripts/)", "one human-readable transcript per scenario, original and confirmation run"]]), "",
           "## 1. Preflight", "",
           f"Outcome: **{'no drift — the deployed runtime equals the accepted Phase 4E state' if pre['frozen_state_match'] else 'RUNTIME DRIFT'}**. "
           f"Checked {pre['checked_at_utc']} (read-only).", "",
           *_table(["Item", "Evidence"], [[k, v] for k, v in pre["evidence"].items()]), "",
           "## 2. Git", "",
           *_table(["Item", "Value"], [[k, v] for k, v in manifest["git"].items()]), "",
           "## 3. Run", "",
           *_table(["Item", "Value"], [
               ["Run id", f"`{res['_meta']['run_id']}`; confirmation `{res['_meta']['confirmation_run_id']}`"],
               ["Window (UTC)", f"{run['original_window_utc']}; confirmation {run['confirmation_window_utc']}"],
               ["Scenarios executed", f"{s['scenarios_executed']} of 15, each exactly once"],
               ["User turns", f"{s['user_turns_executed']} of 53, sent exactly as in `acceptance_scenarios.json`"],
               ["Sessions", f"{s['sessions']}, one per scenario (`pa4f2-<run>-<scenario>`)"],
               ["Session isolation", run["session_isolation"]],
               ["Effective configuration", run["effective_configuration"]],
               ["Tool calls", f"{s['tool_calls']}; at most {s['max_tool_calls_per_turn']} proposed in one turn; result statuses `{_j(s['tool_result_statuses'])}`"],
               ["Guard decisions", f"`{_j(s['guard_decisions'])}`"],
               ["Runtime / tool errors", run["runtime_errors"]],
               ["Infrastructure events", f"{len(s['infrastructure_events']['original'])} in the original run, {len(s['infrastructure_events']['confirmation'])} in the confirmation run"],
               ["Invalid scenarios", "none"],
               ["Confirmation runs", f"{len(s['confirmation_runs']['scenarios'])} failed scenarios re-executed once in fresh sessions ({s['confirmation_runs']['turns']} turns): "
                                     f"{len(s['confirmation_runs']['failed'])} failed again, {len(s['confirmation_runs']['passed'])} passed"],
               ["Latency per turn", f"median {s['latency_s']['median']} s, max {s['latency_s']['max']} s"],
               ["Cost (n8n estimate)", f"${s['cost_usd_n8n_estimate']['original']} original + ${s['cost_usd_n8n_estimate']['confirmation']} confirmation"]]), "",
           "## 4. Results", ""]
    rows = []
    for sc in res["scenarios"]:
        main = "—" if sc["verdict"] == "PASS" else next(i["description"] for lv in LEVELS for t in sc["turns"] for i in t["issues"] if i["severity"] == lv)
        if sc["verdict"] == "PASS" and sc["minor_classes"]:
            main = "notes: " + ", ".join(sc["minor_classes"])
        rows.append([f"[{sc['scenario_id']}](transcripts/{sc['scenario_id']}.md)", sc["verdict"],
                     sc["severity"] if sc["severity"] != "NONE" else "—", main])
    out += _table(["Scenario", "Verdict", "Highest severity", "Main issue"], rows)
    lv = s["issues_by_severity"]
    out += ["", f"- **PASS: {s['scenarios_passed']} scenarios. FAIL: {s['scenarios_failed']} scenarios.** Turns: {s['turns_passed']} passed, {s['turns_failed']} failed of {s['user_turns_executed']}.",
            f"- **RELEASE_BLOCKER: {lv['RELEASE_BLOCKER']}. MAJOR: {lv['MAJOR']}. MINOR: {lv['MINOR']}.** (issues in the original run)",
            f"- Confirmation run: {len(d['release_blockers']) - lv['RELEASE_BLOCKER']} further RELEASE_BLOCKER.", "",
            "Defect-class distribution (original run):", "",
            *_table(["Class", "Failure", "Group", "RELEASE_BLOCKER", "MAJOR", "MINOR", "MAJOR kind (§4.2)"],
                    [[c, classes[c]["name"], classes[c]["group"], n["RELEASE_BLOCKER"] or "", n["MAJOR"] or "", n["MINOR"] or "",
                      (d["major_classes"].get(c) or {}).get("kind", "")] for c, n in s["issues_by_class"].items()]), "",
            "Issues above MINOR by class group: " + ", ".join(f"{g} {n}" for g, n in s["issues_above_minor_by_group"].items()) + ".", "",
            "What held in both runs:", "", *[f"- {x}" for x in review["what_held"]], "",
            "## 5. Acceptance dimensions", "",
            "The decision rule counts scenarios: a scenario that fails counts against every dimension it covers, whatever the "
            "reason. Read together with the class groups: the issues above MINOR of the original run are "
            + ", ".join(f"{n} {g}" for g, n in sorted(s["issues_above_minor_by_group"].items(), key=lambda x: -x[1]))
            + "; none in the " + ", ".join(sorted({c["group"] for c in classes.values()} - set(s["issues_above_minor_by_group"]))) + " groups.", "",
            *_table(["Dimension", "Scenarios passed / total", "At least half", "Failed scenarios"],
                    [[f"{k}. {dims[k]}", f"{v['passed']} / {v['total']}", "yes" if v["at_least_half"] else "**no**" + (" (all failed)" if v["all_failed"] else ""),
                      ", ".join(v["failed"]) or "—"] for k, v in d["dimensions"].items()]), "",
            "## 6. Failed scenarios", "",
            "Every issue above MINOR, with the Consultant's statement, the evidence and the rule it breaks. The full conversations "
            "are in the transcripts.", ""]
    for sc in res["scenarios"]:
        if sc["verdict"] != "FAIL":
            continue
        conf = confirmed.get(sc["scenario_id"])
        out += [f"### {sc['scenario_id']} — {sc['title']}", "",
                f"Failing turns: {', '.join(map(str, sc['failing_turns']))}. {sc['note']} [Transcript](transcripts/{sc['scenario_id']}.md).", ""]
        for t in sc["turns"]:
            for i in t["issues"]:
                if i["severity"] != "MINOR":
                    out += [f"- **Turn {t['turn']} · {i['class']} ({classes[i['class']]['name']}) · {i['severity']}**",
                            f"  - User: «{t['user']}»", f"  - Consultant: {i['description']}", f"  - Evidence and rule: {i['evidence']}"]
        out += ["", f"Cause as observed: {review['scenario_assessment'][sc['scenario_id']]['cause']}",
                f"Confirmation run: **{conf['verdict']}**" + (f" ({', '.join(conf['classes'])}; failing turn(s) {', '.join(map(str, conf['failing_turns']))})" if conf["classes"] else "")
                + f". {conf['note']}", ""]
    out += ["## 7. Historical failure modes", ""]
    status_names = {"did_not_reproduce": "did not reproduce", "reproduced": "reproduced", "new_form": "appeared in a new form",
                    "not_exercised": "not exercised"}
    hist = dataset["_meta"]["historical_failure_modes"]
    for status, title in status_names.items():
        members = [(h, v) for h, v in review["historical_failure_modes"].items() if v["status"] == status]
        if members:
            out += [f"**{title.capitalize()} ({len(members)})**", "",
                    *_table(["Id", "Known failure mode", "In this run"], [[h, hist[h]["summary"], v["note"]] for h, v in members]), ""]
    out += ["## 8. Safety", "", *[f"- {x}" for x in manifest["safety"]], "",
            "## 9. Release decision", "",
            "The rule of the design document §4.3, applied by `acceptance_report.decide`:", "",
            *_table(["Check", "Result"], [[k.replace("_", " "), "not assessed" if v is None else "yes" if v else "**no**"]
                                          for k, v in {**d["validity"], **d["rule_checks"]}.items()]), "",
            f"- RELEASE_BLOCKER issues in any recorded run: {len(d['release_blockers'])} — "
            + "; ".join(f"{b['scenario_id']} turn {b['turn']} ({b['class']}, {b['run']} run)" for b in d["release_blockers"]) + ".",
            f"- Failed scenarios: {len(d['failed_scenarios'])} of 15 ({', '.join(d['failed_scenarios'])}).",
            f"- Systemic MAJOR classes: {', '.join(d['systemic_major_classes']) or 'none'}. Reproducible: "
            + (", ".join(c for c, k in d["major_classes"].items() if k["kind"] == "reproducible") or "none") + ". One-off: "
            + (", ".join(c for c, k in d["major_classes"].items() if k["kind"] == "one-off") or "none") + ".",
            f"- Dimensions below half: {', '.join(k for k, v in d['dimensions'].items() if not v['at_least_half']) or 'none'}.",
            "- Whether each fix would be narrow was not assessed: remediation is outside this phase, and the outcome does not depend on it.", ""]
    if d["hold_reasons"]:
        out += ["HOLD conditions met: " + "; ".join(d["hold_reasons"]) + ".", ""]
    out += [f"**Outcome: {d['outcome']}.** The decision is the project owner's (Phase 4F.3); this report proposes it from the counts.", "",
            "### Problems found in the acceptance design", "",
            "Reported separately, as required. The rubric was applied as written and was not changed.", "",
            *_table(["#", "Where", "Issue", "How it was applied"], [[x["id"], x["where"], x["issue"], x["applied"]] for x in review["design_issues"]]), "",
            "### Observations outside the rubric", "",
            *[f"- **{x['id']}.** {x['text']}" for x in review["observations"]], "",
            f"**{d['final_line']}**"]
    return "\n".join(out) + "\n"


def render_all() -> dict:
    dataset = acceptance.load()
    res = build()
    manifest = _load("run_manifest.json")
    files = {OUT / "results.json": json.dumps(res, ensure_ascii=False, indent=1) + "\n", REPORT: render_report(res, manifest, dataset)}
    confirmed = {c["scenario_id"]: c for c in res["confirmation"]}
    for sc in res["scenarios"]:
        files[OUT / "transcripts" / f"{sc['scenario_id']}.md"] = render_transcript(sc, confirmed.get(sc["scenario_id"]), dataset)
    for text in files.values():
        assert not SECRET.search(text), "secret-like text in a generated file"
    return files


def stale_files() -> list:
    return [str(p.relative_to(PROJECT)) for p, text in render_all().items()
            if not p.exists() or p.read_text(encoding="utf-8") != text]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "collect":
        ev = collect(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5])
        print(ev["label"], ev["run_id"], len(ev["scenarios"]), "scenarios,", sum(len(s["turns"]) for s in ev["scenarios"]), "turns")
    elif cmd == "render":
        (OUT / "transcripts").mkdir(parents=True, exist_ok=True)
        generated = render_all()
        for path, text in generated.items():
            path.write_text(text, encoding="utf-8")
        print(f"wrote {len(generated)} files;", json.loads((OUT / "results.json").read_text())["decision"]["final_line"])
    elif cmd == "--check":
        stale = stale_files()
        if stale:
            raise SystemExit(f"stale: {stale}; run python -m evaluation.acceptance_report render")
        print("acceptance results are current")
    else:
        raise SystemExit(__doc__)
