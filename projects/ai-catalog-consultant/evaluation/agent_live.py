"""Gate 4D.2C live Agent evaluation tooling: drive the committed dataset through the deployed (inactive)
n8n workflow, normalise the executions, and add measurement layers on top of ``agent_eval``.

Procedure (as run in Gate 4D.2C; see docs/PHASE_4D_AGENT_RUNTIME.md §17):

1. ``python -m evaluation.agent_live driver <case_id> <out.json> <session_id>`` builds a temporary,
   inactive driver: Manual Trigger -> (Set turn k -> Execute Workflow with the committed
   ``workflows/ai-consultant.json`` inline) per turn. n8n 2.x runs only published database sub-workflows
   from a non-manual execution, and the Consultant workflow stays inactive, hence the inline definition.
   All turns of a case run in one process with one sessionId, so n8n's in-process window memory carries
   that conversation only; each turn is its own execution (own ``?turn=`` tool budget).
2. The driver is deployed with ``tools/n8n-tool`` (``workflows update``) and run with
   ``docker exec -e N8N_RUNNERS_BROKER_PORT=5699 <n8n> n8n execute --id <driver> --rawOutput``.
3. ``python -m evaluation.agent_live extract <case_id> <parent.out> <trace.json>`` fetches each turn's
   sub-execution through the n8n public API (N8N_BASE_URL / N8N_API_KEY from the environment or
   tools/n8n-tool/.env; never printed or stored) and keeps only what scoring needs.
4. ``python -m evaluation.agent_live analyze <dir> <catalog_codes.txt>`` scores with the unchanged
   ``agent_eval.score_run`` and adds: tool-needed accuracy, argument taxonomy (missing explicit /
   invented budget, size, refresh, required/preferred features, extra use cases), per-model claim check,
   leakage check, tool-bound, latency and token statistics.
"""
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT.parents[1]
CONSULTANT_WF = "4d8mXFWGpS5P4t1L"
DRIVER_NAME = "Samsung — 4D.2C Agent Evaluation Driver (temporary, safe to delete)"
PRICE_IN, PRICE_OUT = 0.40 / 1e6, 1.60 / 1e6          # gpt-4.1-mini list price, USD per token
# Model bake-off gate: list prices, USD per token (input, output). Cached-input discounts are not modelled.
MODEL_PRICES = {"gpt-4.1-mini": (PRICE_IN, PRICE_OUT), "gpt-4o-mini": (0.15 / 1e6, 0.60 / 1e6),
                "gpt-4.1": (2.00 / 1e6, 8.00 / 1e6)}
MODEL_NODE = "OpenAI Chat Model"
CASES_FILE = PROJECT / "evaluation/agent_cases.json"


def cases(path=None):
    return {c["id"]: c for c in json.loads(Path(path or CASES_FILE).read_text())["cases"]}


def with_model(consultant: dict, model: str) -> dict:
    """Model bake-off gate: the committed workflow with only the chat model id replaced. It is applied to the
    driver's inline copy, so the deployed Consultant workflow is never switched."""
    node, = [n for n in consultant["nodes"] if n["name"] == MODEL_NODE]
    node["parameters"]["model"].update(value=model, cachedResultName=model)
    return consultant


def build_driver(case_id: str, session_id: str, model: str = None, cases_path=None, turn_gap_s: int = 0) -> dict:
    """Manual Trigger -> (Set turn k -> Execute Workflow inline) x turns. One process, one sessionId per
    case, so n8n's in-process window memory carries the conversation between turns of this case only.
    ``turn_gap_s`` (model bake-off, provider tokens-per-minute limits): an in-process Wait before each later turn;
    it changes when a turn starts, not what the Agent receives. Must stay below n8n's 65 s in-memory limit."""
    assert 0 <= turn_gap_s < 65
    case = cases(cases_path)[case_id]
    consultant = json.loads((PROJECT / "workflows/ai-consultant.json").read_text())
    if model:
        with_model(consultant, model)
    consultant["id"] = CONSULTANT_WF
    code = json.dumps(consultant, ensure_ascii=False)
    nodes = [{"id": "d2c00000-0000-4000-8000-000000000000", "name": "Manual Trigger",
              "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "position": [0, 0], "parameters": {}}]
    connections, prev = {}, "Manual Trigger"
    for k, turn in enumerate(case["turns"], start=1):
        set_name, run_name = f"Turn {k} input", f"Turn {k}"
        nodes.append({"id": f"d2c00000-0000-4000-8000-{k:06d}0001", "name": set_name, "type": "n8n-nodes-base.set",
                      "typeVersion": 3.4, "position": [220 * (2 * k - 1), 0],
                      "parameters": {"mode": "raw", "options": {},
                                     "jsonOutput": json.dumps({"chatInput": turn["user"], "sessionId": session_id},
                                                              ensure_ascii=False)}})
        nodes.append({"id": f"d2c00000-0000-4000-8000-{k:06d}0002", "name": run_name,
                      "type": "n8n-nodes-base.executeWorkflow", "typeVersion": 1.2, "position": [220 * 2 * k, 0],
                      "parameters": {"source": "parameter", "workflowJson": code, "mode": "once",
                                     "options": {"waitForSubWorkflow": True}}})
        if turn_gap_s and k > 1:
            gap = f"Gap before turn {k}"
            nodes.append({"id": f"d2c00000-0000-4000-8000-{k:06d}0003", "name": gap, "type": "n8n-nodes-base.wait",
                          "typeVersion": 1.1, "position": [220 * (2 * k - 1), 180], "webhookId": f"d2c00000-0000-4000-8000-{k:06d}0004",
                          "parameters": {"amount": turn_gap_s, "unit": "seconds"}})
            connections[prev] = {"main": [[{"node": gap, "type": "main", "index": 0}]]}
            prev = gap
        connections[prev] = {"main": [[{"node": set_name, "type": "main", "index": 0}]]}
        connections[set_name] = {"main": [[{"node": run_name, "type": "main", "index": 0}]]}
        prev = run_name
    return {"name": DRIVER_NAME, "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True, "saveDataSuccessExecution": "all",
                         "saveDataErrorExecution": "all", "callerPolicy": "workflowsFromSameOwner"}}


def _api():
    env = dict(os.environ)
    dotenv = REPO / "tools/n8n-tool/.env"
    if "N8N_API_KEY" not in env and dotenv.exists():
        env.update(l.split("=", 1) for l in dotenv.read_text().splitlines() if "=" in l and not l.startswith("#"))
    return env["N8N_BASE_URL"].strip().rstrip("/"), env["N8N_API_KEY"].strip()


def fetch(sub_id: str) -> dict:
    base, key = _api()
    req = urllib.request.Request(f"{base}/api/v1/executions/{sub_id}?includeData=true", headers={"X-N8N-API-KEY": key})
    for attempt in range(5):
        data = json.load(urllib.request.urlopen(req, timeout=30))
        if data.get("finished") or data.get("status") in ("success", "error", "crashed"):
            return data
        time.sleep(2)
    return data


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def trace_turn(sub: dict) -> dict:
    rd = sub["data"]["resultData"]["runData"]
    llm, tokens = [], {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0}
    requested = []          # what n8n sent to the provider per model round: the proof of which model answered
    for r in rd.get(MODEL_NODE, []):
        for item in ((r.get("inputOverride") or {}).get("ai_languageModel") or [[]])[0]:
            o = (item.get("json") or {}).get("options") or {}
            requested.append({k: o.get(k) for k in ("model", "temperature", "max_tokens", "use_responses_api")})
        for item in ((r.get("data") or {}).get("ai_languageModel") or [[]])[0]:
            u = item["json"].get("tokenUsage") or item["json"].get("tokenUsageEstimate") or {}
            for k in tokens:
                tokens[k] += u.get(k, 0)
            llm.append({"ms": r.get("executionTime"), "estimated": "tokenUsage" not in item["json"]})
    tool_runs = []
    for r in rd.get("catalog", []):
        inp = (r.get("inputOverride") or {}).get("ai_tool") or (r.get("inputOverride") or {}).get("main") or [[]]
        outs = ((r.get("data") or {}).get("ai_tool") or (r.get("data") or {}).get("main") or [[]])[0]
        for o in outs:
            resp = o["json"].get("response")
            text = resp[0].get("text") if isinstance(resp, list) and resp else resp
            tool_runs.append({"ms": r.get("executionTime"), "status": r.get("executionStatus"),
                              "payload": json.loads(text) if isinstance(text, str) else text})
        if not outs:
            tool_runs.append({"ms": r.get("executionTime"), "status": r.get("executionStatus"), "payload": None,
                              "error": str((r.get("error") or {}).get("message"))[:200]})
    agent_runs = rd.get("Samsung AI Consultant", [])
    last = agent_runs[-1] if agent_runs else {}
    main = ((last.get("data") or {}).get("main") or [[]])[0]
    item = main[0]["json"] if main else {}
    models = sorted({str(o["model"]) for o in requested if o.get("model")})
    price_in, price_out = MODEL_PRICES.get(models[0], (PRICE_IN, PRICE_OUT)) if len(models) == 1 else (PRICE_IN, PRICE_OUT)
    return {"sub_execution": sub.get("id"), "status": sub.get("status"), "models": models,
            "model_options": [dict(t) for t in sorted({tuple(sorted(o.items(), key=str)) for o in requested}, key=str)],
            "error": str(((sub["data"]["resultData"].get("error")) or {}).get("message", ""))[:300] or None,
            "latency_s": round((_ts(sub["stoppedAt"]) - _ts(sub["startedAt"])).total_seconds(), 2),
            "model_rounds": len(llm), "llm_ms": [x["ms"] for x in llm], "tokens": tokens,
            "tokens_estimated": all(x["estimated"] for x in llm) if llm else None,
            "cost_usd": round(tokens["promptTokens"] * price_in + tokens["completionTokens"] * price_out, 6),
            "tool_ms": [t["ms"] for t in tool_runs], "agent_item": item}


def extract(case_id: str, parent_out: Path, out: Path, cases_path=None) -> dict:
    raw = parent_out.read_text()
    parent, _ = json.JSONDecoder().raw_decode(raw[raw.index("{\n"):])   # a failed execution prints its error after the JSON
    rd = parent["data"]["resultData"]["runData"]
    parent_error = parent["data"]["resultData"].get("error") or {}
    turns = []
    for k, turn in enumerate(cases(cases_path)[case_id]["turns"], start=1):
        runs = rd.get(f"Turn {k}")
        if not runs:
            turns.append({"user": turn["user"], "missing": True, "parent_error": str(parent_error.get("message"))[:300]})
            continue
        sub_id = (runs[0].get("metadata") or {}).get("subExecution", {}).get("executionId")
        # A turn whose sub-execution failed has no metadata: keep the provider's own description (e.g. no credits).
        t = trace_turn(fetch(sub_id)) if sub_id else {"missing": True, "parent_error": " | ".join(
            str(parent_error.get(k))[:300] for k in ("message", "description") if parent_error.get(k))}
        turns.append({"user": turn["user"], **t})
    rec = {"case": case_id, "parent_status": parent.get("status"), "turns": turns}
    text = json.dumps(rec, ensure_ascii=False, indent=1)
    base, key = _api()
    assert key not in text and not re.search(r"(?i)bearer\s+[A-Za-z0-9]{16}", text)
    out.write_text(text)
    return rec

from .agent_eval import (MODEL_CODE, MONEY, UNIT_NUMBER, from_n8n_agent_output, invented_arguments,  # noqa: E402
                         load_cases, product_prices, score_run)

FEATURE_PATTERNS = {"hz_120": (r"120\s*Гц",), "vrr": (r"\bVRR\b",), "freesync_premium": (r"FreeSync",), "allm": (r"\bALLM\b",),
                    "game_bar": (r"игров\w* (панел|режим)", r"Game Bar"), "hdmi_2_1": (r"HDMI 2\.1",), "earc": (r"eARC",),
                    "anti_glare": (r"антиблик",), "filmmaker_mode": (r"Filmmaker|режим\w* режисс",), "dolby_atmos": (r"Atmos",)}
HEDGE = re.compile(r"(?i)не указ|не подтвержд|нет данных|может|могут|не (явно|точно)|неизвестн|возможно")
LEAK = re.compile(r"(?i)\b[PA]\d{1,2}\b(?![\"″])|similarity|product_id|external_id|embedding|chunk_id|agent-result-v1|"
                  r"catalog_[a-z_]+\b|\bSELECT\b.*\bFROM\b|Bearer|CONSULTANT_|samsung-consultant:8765")
SQL_CLAIM = re.compile(r"(?i)(выполнил|выполнен|результат)\w*[^.]{0,40}(sql|запрос)")


def claim_check(answer: str, results: list) -> tuple:
    """Per-model claims on lines naming exactly one product (code or its product link): price must be the
    live price or list price; 'в наличии' must match availability; feature words must be 'yes' (or be in the
    product's spec rows / typed specs). Hedged lines are skipped for features. Returns (checked, issues)."""
    prods = {}
    for r in results:
        for x in (r or {}).get("products") or []:
            prods[x["model_code"]] = x
        for x in (r or {}).get("alternatives") or []:
            prods.setdefault(x["model_code"], x)
    checked, issues = 0, []
    for line in re.split(r"\n+", answer):
        found = set(MODEL_CODE.findall(line)) | {c for c in prods if f"/product/{c}/" in line}
        if len(found) != 1:
            continue
        code = found.pop()
        x = prods.get(code)
        if x is None:
            # Gate 4D.2D: naming a code (e.g. the user's own) is not a claim; a price, availability, feature or
            # unit value on that line is.
            if (MONEY.search(line) or UNIT_NUMBER.search(line) or re.search(r"(?i)в наличии", line)
                    or any(re.search(p, line, re.I) for pats in FEATURE_PATTERNS.values() for p in pats)):
                issues.append(f"{code}: named on a line but not in this session's evidence")
            continue
        spec_text = json.dumps(x.get("catalog_specs", []) + [x.get("name", ""), x.get("specs", {})], ensure_ascii=False)
        for m in MONEY.finditer(line):
            v = int(re.sub(r"\D", "", m.group(1)))
            checked += 1
            if v not in product_prices(x):
                issues.append(f"{code}: price {v} not in evidence")
        if re.search(r"(?i)в наличии", line) and not re.search(r"(?i)нет в наличии|не в наличии", line):
            checked += 1
            if x.get("available") is not True:
                issues.append(f"{code}: 'в наличии' but available={x.get('available')}")
        if HEDGE.search(line):
            continue
        for fid, pats in FEATURE_PATTERNS.items():
            if any(re.search(p, line, re.I) for p in pats):
                checked += 1
                st = (x.get("features") or {}).get(fid)
                st = st if isinstance(st, str) or st is None else st.get("state")
                if fid == "hz_120" and (x.get("specs") or {}).get("refresh_rate_hz") == 120:
                    continue
                if fid == "freesync_premium" and re.search(r"Premium Pro", line):
                    ok = "Premium Pro" in spec_text
                else:
                    ok = st == "yes" or (st is None and any(re.search(p, spec_text, re.I) for p in pats))
                if not ok:
                    issues.append(f"{code}: {fid} claimed, evidence state={st}")
    return checked, issues


def arg_taxonomy(spec: dict, calls: list, user_text: str) -> dict:
    exp_args = spec.get("args") or {}
    target = next((c for c in calls if c["tool"] == spec.get("tool")), calls[0] if calls else None)
    args = (target or {}).get("args") or {}
    missing = [k for k, v in exp_args.items() if k not in args] if target else []
    invented = [x for c in calls for x in invented_arguments(c.get("args"), user_text)]
    cats = {"budget": [x for x in invented if x.startswith(("max_price", "min_price"))],
            "size": [x for x in invented if "screen_size" in x],
            "refresh": [x for x in invented if x.startswith("min_refresh")],
            "required_features": [x for x in invented if x.startswith("required_features")],
            "preferred_features": [x for x in invented if x.startswith("preferred_features")]}
    exp_uc = set(exp_args.get("use_cases") or [])
    extra_uc = sorted({u for c in calls for u in (c.get("args") or {}).get("use_cases") or []} - exp_uc) \
        if spec.get("tool") == "recommend_tvs" and exp_uc else []
    cats["extra_use_cases"] = extra_uc
    return {"missing_explicit": missing, "invented": cats, "invented_total": sum(len(v) for v in cats.values())}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


def analyze(out_dir: Path, catalog_codes: frozenset, cases_path=None) -> dict:
    OUT, CODES = Path(out_dir), catalog_codes
    cases = load_cases(Path(cases_path)) if cases_path else load_cases()
    transcripts, traces, extra = {}, {}, {}
    for c in cases:
        f = OUT / f"{c['id']}.trace.json"
        if not f.exists():
            continue
        tr = json.loads(f.read_text())
        traces[c["id"]] = tr
        turns = []
        for t in tr["turns"]:
            turns.append(from_n8n_agent_output(t["user"], t.get("agent_item") or {}))
        transcripts[c["id"]] = {"turns": turns}
    run = score_run(cases, transcripts, CODES)
    by_key = {(t["case_id"], t["turn"]): t for t in run["turns"]}
    rows = []
    for c in cases:
        if c["id"] not in transcripts:
            continue
        session_users, session_results = [], []
        for i, (spec, turn) in enumerate(zip(c["turns"], transcripts[c["id"]]["turns"])):
            tt = traces[c["id"]]["turns"][i]
            session_users.append(spec["user"])
            results = [x["result"] for x in turn["tool_calls"]]
            session_results.extend(results)
            sc = by_key[(c["id"], i)]
            checked, issues = claim_check(turn["answer"], session_results)
            statuses = [(x.get("result") or {}).get("status") for x in turn["tool_calls"]]
            expected_tool = spec["tool"]
            acceptable = spec.get("acceptable_tools", [])
            called = bool(turn["tool_calls"])
            needed_ok = (called == (expected_tool is not None)) or (not called and None in acceptable) or \
                        (called and expected_tool is None and turn["tool_calls"][0]["tool"] in acceptable)
            g = sc["grounding"]
            hard_flags = {k: g[k] for k in ("fabricated_models", "ungrounded_models", "fabricated_prices", "unsupported_numbers",
                                            "fabricated_features", "catalog_claims_without_evidence", "availability_mismatches",
                                            "aggregate_claim_flags", "mislabelled_prices", "unsupported_comparatives",
                                            "forbidden_pattern_hits") if g.get(k)}
            soft_flags = {k: g[k] for k in ("lost_gaps", "missing_required_mentions") if g.get(k)}
            leaks = sorted(set(m.group(0) for m in LEAK.finditer(turn["answer"])))
            rows.append({
                "case_id": c["id"], "turn": i, "family": c["family"], "user": spec["user"],
                "expected_tool": expected_tool, "acceptable_tools": acceptable, "expected_args": spec.get("args"),
                "clarification_expected": spec.get("clarification", "optional"),
                "tools": [x["tool"] for x in turn["tool_calls"]],
                "args": [{k: v for k, v in (x.get("args") or {}).items() if k != "tool"} for x in turn["tool_calls"]],
                "statuses": statuses, "calls": len(turn["tool_calls"]),
                "blocked_by_limit": statuses.count("tool_call_limit_reached"),
                "tool_needed_ok": needed_ok, "selection": sc["selection"], "arguments": sc["arguments"],
                "argument_notes": sc["argument_notes"], "arg_taxonomy": arg_taxonomy(spec, turn["tool_calls"], " ".join(session_users)),
                "clarification_asked": sc["clarification_asked"], "clarification_ok": sc["clarification_ok"],
                "grounding_hard_flags": hard_flags, "grounding_soft_flags": soft_flags,
                "claims_checked": checked, "claim_issues": issues, "leaks": leaks,
                "sql_execution_claim": bool(SQL_CLAIM.search(turn["answer"])),
                "manual_review": g.get("manual_review", []),
                "latency_s": tt.get("latency_s"), "model_rounds": tt.get("model_rounds"), "tool_ms": tt.get("tool_ms"),
                "tokens": tt.get("tokens"), "tokens_estimated": tt.get("tokens_estimated"), "cost_usd": tt.get("cost_usd"),
                "execution_status": tt.get("status"), "execution_error": tt.get("error"), "sub_execution": tt.get("sub_execution"),
                "models": tt.get("models"),
                "answer": turn["answer"],
                "tool_results": [{"tool": x["tool"], "status": (x.get("result") or {}).get("status"),
                                  "confidence": (x.get("result") or {}).get("confidence"),
                                  "gaps": [gg.get("kind") for gg in (x.get("result") or {}).get("gaps") or []],
                                  "products": [p["model_code"] for p in (x.get("result") or {}).get("products") or []],
                                  "alternatives": [p["model_code"] for p in (x.get("result") or {}).get("alternatives") or []],
                                  "totals": (x.get("result") or {}).get("totals"),
                                  "clarification": (x.get("result") or {}).get("clarification")} for x in turn["tool_calls"]],
            })
    for r in rows:
        r["auto_pass"] = (r["selection"] != "wrong" and r["arguments"] != "wrong" and r["clarification_ok"]
                          and not r["grounding_hard_flags"] and not r["claim_issues"] and not r["leaks"]
                          and r["execution_status"] == "success" and r["calls"] <= 3)
    head = [r for r in rows if r["family"] != "followup"]
    lat = [r["latency_s"] for r in rows if r["latency_s"] is not None]
    tok = [r["tokens"]["totalTokens"] for r in rows if r.get("tokens")]
    summary = {
        "cases_run": len(transcripts), "turns_run": len(rows), "missing": run["missing_cases"],
        "models": sorted({m for r in rows for m in r["models"] or []}),
        "committed_scorer_headline": run["headline"], "committed_scorer_followup": run["followup_spike"],
        "tool_needed_accuracy": f"{sum(r['tool_needed_ok'] for r in head)}/{len(head)}",
        "invented_args_turns": sorted(f"{r['case_id']}[{r['turn']}]" for r in rows if r["arg_taxonomy"]["invented_total"]),
        "invented_by_category": {k: sum(len(r["arg_taxonomy"]["invented"][k]) for r in rows)
                                 for k in ("budget", "size", "refresh", "required_features", "preferred_features", "extra_use_cases")},
        "missing_explicit_turns": sorted(f"{r['case_id']}[{r['turn']}]: {r['arg_taxonomy']['missing_explicit']}"
                                         for r in rows if r["arg_taxonomy"]["missing_explicit"]),
        "max_calls": max(r["calls"] for r in rows), "turns_at_limit": [f"{r['case_id']}[{r['turn']}]" for r in rows if r["calls"] >= 3],
        "blocked_calls": sum(r["blocked_by_limit"] for r in rows),
        "claims_checked": sum(r["claims_checked"] for r in rows),
        "claim_issues": {f"{r['case_id']}[{r['turn']}]": r["claim_issues"] for r in rows if r["claim_issues"]},
        "hard_flags": {f"{r['case_id']}[{r['turn']}]": r["grounding_hard_flags"] for r in rows if r["grounding_hard_flags"]},
        "soft_flags": {f"{r['case_id']}[{r['turn']}]": r["grounding_soft_flags"] for r in rows if r["grounding_soft_flags"]},
        "leaks": {f"{r['case_id']}[{r['turn']}]": r["leaks"] for r in rows if r["leaks"]},
        "execution_failures": [f"{r['case_id']}[{r['turn']}]: {r['execution_error']}" for r in rows if r["execution_status"] != "success"],
        "auto_failures": [f"{r['case_id']}[{r['turn']}]" for r in rows if not r["auto_pass"]],
        "latency_s": {"median": statistics.median(lat), "p90": pct(lat, 0.9), "p95": pct(lat, 0.95), "max": max(lat)},
        "model_rounds": {"max": max(r["model_rounds"] or 0 for r in rows), "distribution": {
            str(k): sum(1 for r in rows if r["model_rounds"] == k) for k in sorted({r["model_rounds"] for r in rows})}},
        "tool_ms": {"median": statistics.median([m for r in rows for m in (r["tool_ms"] or [])] or [0]),
                    "max": max([m for r in rows for m in (r["tool_ms"] or [])] or [0])},
        "tokens": {"total": sum(tok), "prompt": sum(r["tokens"]["promptTokens"] for r in rows),
                   "completion": sum(r["tokens"]["completionTokens"] for r in rows), "median_per_turn": statistics.median(tok),
                   "max_per_turn": max(tok), "estimated": all(r["tokens_estimated"] in (True, None) for r in rows)},
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        "top_cost": sorted(((r["cost_usd"], f"{r['case_id']}[{r['turn']}]") for r in rows), reverse=True)[:5],
        "slowest": sorted(((r["latency_s"], f"{r['case_id']}[{r['turn']}]") for r in rows), reverse=True)[:5],
    }
    result = {"summary": summary, "turns": rows}
    (OUT / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result


def load_catalog_codes(path: Path) -> frozenset:
    """``CODE|<model_code>|...`` lines from a read-only catalog query (see §17)."""
    return frozenset(l.split("|")[1] for l in Path(path).read_text().splitlines() if l.startswith("CODE|"))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "driver":
        case_id, out, session = sys.argv[2], sys.argv[3], sys.argv[4]
        Path(out).write_text(json.dumps(build_driver(case_id, session), ensure_ascii=False, indent=1))
    elif cmd == "extract":
        rec = extract(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
        for t in rec["turns"]:
            print(rec["case"], t.get("status"), t.get("latency_s"), t.get("model_rounds"), len(t.get("tool_ms") or []))
    elif cmd == "analyze":
        print(json.dumps(analyze(Path(sys.argv[2]), load_catalog_codes(Path(sys.argv[3])))["summary"], ensure_ascii=False, indent=1))
    else:
        raise SystemExit(__doc__)
