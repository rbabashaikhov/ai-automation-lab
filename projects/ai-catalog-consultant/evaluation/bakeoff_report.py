"""Model bake-off gate: post-processing only (no n8n, no OpenAI, no DB).

    python -m evaluation.bakeoff_report <runs_dir> <catalog_codes.txt> <consultant_log.txt> [manual.json]

Reads the traces written by ``model_bakeoff run``, scores them with the unchanged ``agent_live.analyze`` /
``agent_eval``, joins the semantic guard's own decision for every tool call (the Consultant's ``tool_call`` log
line, matched by the turn key = n8n execution id) and writes

* ``results/model_bakeoff.json``                -- per-model metrics and the per-turn verdict table;
* ``results/model_bakeoff_conversations.json``  -- Conversation Audit Log: every scored dialogue of every model;
* ``results/model_bakeoff_conversations.md``    -- the same log, readable.

The audit log holds what was said and done, never model reasoning: user message, final answer, tool, the
arguments the model proposed, the guard decision, the arguments the Core received, a compact tool result,
latency, token usage, estimated cost, automated and manual verdicts.
"""
import ast
import json
import re
import statistics
import sys
from pathlib import Path

from .agent_eval import RETRY_STATUSES, from_n8n_agent_output, product_prices
from .agent_live import PROJECT, analyze, load_catalog_codes, pct
from .model_bakeoff import FROZEN_CASES, MODEL_PRICES, MODELS, SUPPLEMENT_CASES, manifest

RESULTS = PROJECT / "evaluation/results"
SETS = (("frozen", FROZEN_CASES), ("supplement", SUPPLEMENT_CASES))
REQUEST_LINE = re.compile(r'"POST /mcp\?turn=(\d+)&conv=([^&\s]+)')
TOOL_CALL_LINE = re.compile(r"consultant\.agent_tools tool_call (\{.*\})\s*$")
HARD_FLAG_KEYS = ("fabricated_models", "ungrounded_models", "fabricated_prices", "unsupported_numbers", "fabricated_features",
                  "catalog_claims_without_evidence", "availability_mismatches", "aggregate_claim_flags", "mislabelled_prices",
                  "unsupported_comparatives", "forbidden_pattern_hits")
INVENTED_KEYS = ("budget", "size", "refresh", "required_features", "preferred_features", "extra_use_cases")


def guard_decisions(log_text: str) -> dict:
    """``{turn key: [decision, ...]}`` in call order. The server logs a call's ``tool_call`` line and then the
    request line of the POST that carried it; the log has argument names, values and reason codes, never text."""
    out, pending = {}, []
    for line in log_text.splitlines():
        m = TOOL_CALL_LINE.search(line)
        if m:
            pending.append(ast.literal_eval(m.group(1)))
            continue
        m = REQUEST_LINE.search(line)
        if m and pending:
            d = pending.pop(0)
            out.setdefault(m.group(1), []).append(
                {"tool": d.get("tool"), "result_status": d.get("status"), "status": d.get("guard", "not_run"),
                 "detail": d.get("detail"),            # not_run: the call was refused before the guard (per-turn cap)
                 "actions": [{"action": a[0], "argument": a[1], "value": a[2], "reason": a[3]} for a in d.get("actions", [])]})
    return out


def final_arguments(original, guard: dict):
    """The arguments the Core received: the proposal minus what the guard removed, plus what it added.
    ``report_only`` / ``unchanged`` / ``skipped`` leave the proposal as it is."""
    if not isinstance(original, dict) or not guard or guard["status"] != "modified":
        return original
    args = json.loads(json.dumps(original))
    for a in guard["actions"]:
        key, value = a["argument"], a["value"]
        if a["action"] == "removed":
            if isinstance(args.get(key), list) and not isinstance(value, list):
                args[key] = [x for x in args[key] if x != value]
                if not args[key]:
                    del args[key]
            else:
                args.pop(key, None)
        elif a["action"] == "added":
            if isinstance(args.get(key), list) or (key not in args and not isinstance(value, (list, int, float))
                                                   and key.endswith(("s", "features"))):
                args[key] = [*args.get(key, []), *(value if isinstance(value, list) else [value])]
            else:
                args[key] = value
    return args


def compact_result(r) -> dict:
    if not isinstance(r, dict):
        return {"status": None}
    def prod(p):                                                             # noqa: E306
        current, before = product_prices(p)
        return {"model_code": p.get("model_code"), "current_price_rub": current,
                **({"price_before_discount_rub": before} if before is not None else {}), "available": p.get("available")}
    req = r.get("request") or {}
    out = {"status": r.get("status"), "confidence": r.get("confidence"), "totals": r.get("totals"),
           "applied_request": {k: req[k] for k in ("constraints", "use_cases", "required_features", "preferred_features",
                                                   "attributes_asked", "sort", "question") if req.get(k)},
           "gaps": [g.get("kind") for g in r.get("gaps") or []],
           "products": [prod(p) for p in r.get("products") or []],
           "alternatives": [prod(p) for p in r.get("alternatives") or []]}
    for key, value in (("not_applied", req.get("not_applied")), ("model_resolution", req.get("model_resolution")),
                       ("clarification", r.get("clarification")), ("errors", r.get("errors"))):
        if value:
            out[key] = value
    return out


def automated_reasons(row: dict) -> list:
    why = []
    if row["selection"] == "wrong":
        why.append(f"tool selection: expected {row['expected_tool']}, got {row['tools'] or 'no tool'}")
    if row["arguments"] == "wrong":
        why.extend(row["argument_notes"] or ["arguments differ from the label"])
    if not row["clarification_ok"]:
        why.append(f"clarification {row['clarification_expected']}, asked={row['clarification_asked']}")
    why.extend(f"{k}: {v}" for k, v in row["grounding_hard_flags"].items())
    why.extend(f"claim: {x}" for x in row["claim_issues"])
    why.extend(f"leak: {x}" for x in row["leaks"])
    if row["execution_status"] != "success":
        why.append(f"execution {row['execution_status']}: {row['execution_error']}")
    if row["calls"] > 3:
        why.append(f"{row['calls']} tool calls")
    return why


def collect(runs: Path, codes: frozenset, decisions: dict, manual: dict) -> list:
    """One record per scored turn: by model, stage 1 before the full run, cases in dataset order."""
    turns = []
    for model in MODELS:
        for run_dir in sorted((p for p in (runs / model).glob("*") if p.is_dir() and not p.name.startswith("smoke")),
                              key=lambda p: (not p.name.startswith("stage1"), p.name)):       # stage 1 before the full run
            for set_name, cases_path in SETS:
                d = run_dir / set_name
                if not d.is_dir() or not list(d.glob("*.trace.json")):
                    continue
                rows = analyze(d, codes, cases_path)["turns"]
                traces = {p.name[:-len(".trace.json")]: json.loads(p.read_text()) for p in d.glob("*.trace.json")}
                for row in rows:
                    trace = traces[row["case_id"]]
                    tt = trace["turns"][row["turn"]]
                    norm = from_n8n_agent_output(tt["user"], tt.get("agent_item") or {})
                    guards = list(decisions.get(str(row["sub_execution"]), []))
                    calls = []
                    for call in norm["tool_calls"]:
                        original = {k: v for k, v in (call.get("args") or {}).items() if k != "tool"} \
                            if isinstance(call.get("args"), dict) else call.get("args")
                        # Parallel calls are logged in arrival order, not in the Agent's order: match by tool and outcome.
                        status = (call.get("result") or {}).get("status")
                        g = next((x for x in guards if x["tool"] == call["tool"] and x["result_status"] == status), None)
                        if g is not None:
                            guards.remove(g)
                        calls.append({"tool": call["tool"], "original_args": original,
                                      "guard": {k: g[k] for k in ("status", "detail", "actions")} if g else {"status": "not_found_in_log"},
                                      "final_args": final_arguments(original, g), "result": compact_result(call.get("result"))})
                    retries = sum(1 for s in row["statuses"] if s in RETRY_STATUSES)
                    needed = 1 if row["expected_tool"] is not None or (row["tools"] and row["tools"][0] in row["acceptable_tools"]) else 0
                    key = f"{model}/{run_dir.name}/{row['case_id']}[{row['turn']}]"
                    turns.append({
                        "key": key, "model": model, "run": run_dir.name, "set": set_name, "case_id": row["case_id"],
                        "turn": row["turn"], "family": row["family"], "session": trace.get("session"),
                        "execution": row["sub_execution"], "case_started": trace.get("started"),
                        "requested": tt.get("model_options"),
                        "user": row["user"], "assistant": row["answer"], "tool_calls": calls,
                        "expected": {"tool": row["expected_tool"], "acceptable_tools": row["acceptable_tools"],
                                     "args": row["expected_args"], "clarification": row["clarification_expected"]},
                        "latency_s": row["latency_s"], "model_rounds": row["model_rounds"], "tool_ms": row["tool_ms"],
                        "tokens": {**(row["tokens"] or {}), "estimated_by_n8n": row["tokens_estimated"]},
                        "cost_usd": row["cost_usd"],
                        "automated": {"pass": row["auto_pass"], "reasons": automated_reasons(row), "selection": row["selection"],
                                      "arguments": row["arguments"], "invented_arguments": row["arg_taxonomy"]["invented"],
                                      "missing_explicit": row["arg_taxonomy"]["missing_explicit"],
                                      "soft_flags": row["grounding_soft_flags"], "claims_checked": row["claims_checked"],
                                      "invalid_calls": retries, "unnecessary_calls": max(0, row["calls"] - retries - needed),
                                      "blocked_by_cap": row["blocked_by_limit"],
                                      "hard_flags": row["grounding_hard_flags"], "claim_issues": row["claim_issues"],
                                      "leaks": row["leaks"], "execution_status": row["execution_status"]},
                        "manual": manual.get(key, {"verdict": "not_reviewed"}),
                    })
    return turns


def metrics(turns: list) -> dict:
    """Aggregates over one model's turns of one run (or any subset)."""
    if not turns:
        return {}
    lat = [t["latency_s"] for t in turns if t["latency_s"] is not None]
    calls = [c for t in turns for c in t["tool_calls"]]
    case_ok = {}
    for t in turns:
        case_ok[t["case_id"]] = case_ok.get(t["case_id"], True) and t["manual"].get("verdict") == "pass"
    guard = {}
    for c in calls:
        guard[c["guard"]["status"]] = guard.get(c["guard"]["status"], 0) + 1
    reviewed = all(t["manual"].get("verdict") in ("pass", "fail") for t in turns)
    mc = lambda k: sum(int((t["manual"].get("counts") or {}).get(k, 0)) for t in turns)      # noqa: E731
    return {
        "turns": len(turns), "cases": len(case_ok), "multi_turn_cases": len({t["case_id"] for t in turns if t["turn"] > 0}),
        "manual": {"reviewed": reviewed, "turns_passed": sum(t["manual"].get("verdict") == "pass" for t in turns),
                   "cases_passed": sum(case_ok.values()),
                   "failed_turns": [f"{t['case_id']}[{t['turn']}]" for t in turns if t["manual"].get("verdict") == "fail"],
                   "counts": {k: mc(k) for k in ("invented_hard_constraints", "invented_prices", "invented_sizes",
                                                 "invented_required_features", "wrong_model_ids", "wrong_filter_values",
                                                 "explicit_constraints_lost", "fabricated_models", "fabricated_prices",
                                                 "fabricated_specs", "unsupported_numeric_claims", "unsupported_comparisons",
                                                 "wrong_price_basis")}},
        "automated": {"turns_passed": sum(t["automated"]["pass"] for t in turns),
                      "failed_turns": [f"{t['case_id']}[{t['turn']}]" for t in turns if not t["automated"]["pass"]]},
        "tools": {"selection": {k: sum(t["automated"]["selection"] == k for t in turns) for k in ("exact", "acceptable", "wrong")},
                  "calls": len(calls), "max_calls_per_turn": max(len(t["tool_calls"]) for t in turns),
                  "invalid_calls": sum(t["automated"]["invalid_calls"] for t in turns),
                  "unnecessary_calls": sum(t["automated"]["unnecessary_calls"] for t in turns),
                  "cap_violations": sum(len(t["tool_calls"]) > 3 for t in turns),
                  "blocked_by_cap": sum(t["automated"]["blocked_by_cap"] for t in turns),
                  "execution_failures": [f"{t['case_id']}[{t['turn']}]" for t in turns
                                         if t["automated"]["execution_status"] != "success"]},
        "arguments": {"exact": sum(t["automated"]["arguments"] == "exact" for t in turns),
                      "acceptable": sum(t["automated"]["arguments"] == "acceptable" for t in turns),
                      "wrong": sum(t["automated"]["arguments"] == "wrong" for t in turns),
                      "invented_by_category": {k: sum(len(t["automated"]["invented_arguments"][k]) for t in turns)
                                               for k in INVENTED_KEYS},
                      "invented_turns": [f"{t['case_id']}[{t['turn']}]" for t in turns
                                         if any(t["automated"]["invented_arguments"][k] for k in INVENTED_KEYS)],
                      "missing_explicit_turns": [f"{t['case_id']}[{t['turn']}]: {t['automated']['missing_explicit']}"
                                                 for t in turns if t["automated"]["missing_explicit"]]},
        "guard": {"decisions": guard,
                  "removed": [f"{t['case_id']}[{t['turn']}]: {a['argument']}={a['value']}" for t in turns
                              for c in t["tool_calls"] for a in c["guard"].get("actions", []) if a["action"] == "removed"],
                  "would_remove": [f"{t['case_id']}[{t['turn']}]: {a['argument']}={a['value']}" for t in turns
                                   for c in t["tool_calls"] for a in c["guard"].get("actions", [])
                                   if a["action"] == "would_remove"]},
        "grounding_flags": {k: sum(len(t["automated"]["hard_flags"].get(k, [])) for t in turns) for k in HARD_FLAG_KEYS},
        "claims": {"checked": sum(t["automated"]["claims_checked"] for t in turns),
                   "issues": sum(len(t["automated"]["claim_issues"]) for t in turns)},
        "leaks": sum(len(t["automated"]["leaks"]) for t in turns),
        "latency_s": {"median": statistics.median(lat), "p90": pct(lat, 0.9), "p95": pct(lat, 0.95), "max": max(lat),
                      "mean": round(statistics.mean(lat), 2)},
        "model_rounds": {str(k): sum(t["model_rounds"] == k for t in turns) for k in sorted({t["model_rounds"] for t in turns})},
        "tokens": {"input": sum(t["tokens"].get("promptTokens", 0) for t in turns),
                   "output": sum(t["tokens"].get("completionTokens", 0) for t in turns),
                   "answer_chars_median": statistics.median(len(t["assistant"]) for t in turns)},
        "cost_usd": {"total": round(sum(t["cost_usd"] or 0 for t in turns), 4),
                     "per_turn": round(sum(t["cost_usd"] or 0 for t in turns) / len(turns), 5)},
    }


def conversations(turns: list) -> list:
    out, index = [], {}
    for t in turns:
        key = (t["model"], t["run"], t["case_id"])
        if key not in index:
            index[key] = {"model": t["model"], "run": t["run"], "set": t["set"], "case_id": t["case_id"], "family": t["family"],
                          "session": t["session"], "started": t["case_started"], "turns": []}
            out.append(index[key])
        index[key]["turns"].append({k: t[k] for k in ("turn", "execution", "requested", "user", "assistant", "tool_calls",
                                                       "expected", "latency_s", "model_rounds", "tokens", "cost_usd",
                                                       "automated", "manual")})
    for c in out:
        verdicts = {t["manual"].get("verdict") for t in c["turns"]}
        c["case_manual"] = "fail" if "fail" in verdicts else "pass" if verdicts == {"pass"} else "not_reviewed"
        c["case_passed_automated"] = all(t["automated"]["pass"] for t in c["turns"])
    return out


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def _products(ps: list) -> str:
    return ", ".join(f"{p['model_code']} {p['current_price_rub']:,}".replace(",", " ") + " ₽"
                     + (f" (до скидки {p['price_before_discount_rub']:,})".replace(",", " ") if p.get("price_before_discount_rub") else "")
                     + ("" if p.get("available") else " [нет в наличии]") if isinstance(p.get("current_price_rub"), (int, float))
                     else f"{p['model_code']}" for p in ps) or "—"


def render_markdown(convs: list, meta: dict) -> str:
    lines = ["# Model bake-off — Conversation Audit Log", "",
             "Every scored dialogue of every model: by model, stage 1 before the full run, cases in dataset order. Generated by `python -m evaluation.bakeoff_report` from the "
             "run traces; the JSON twin is `model_bakeoff_conversations.json`.", "",
             "- Only the chat model differs between models; the prompt, tools, guard, memory, scorer and datasets are frozen "
             f"(commit `{meta.get('frozen_commit', '')}`).",
             "- *Original args* are what the model proposed; *guard* is the semantic guard's own logged decision for that call; "
             "*final args* are what the Core received.",
             "- Tokens and cost are n8n's estimates at list prices (the same method for every model).",
             "- The log contains user messages, final answers and tool data only. No model reasoning is recorded.", ""]
    for model in MODELS:
        for run in sorted({c["run"] for c in convs if c["model"] == model}, key=lambda r: (not r.startswith("stage1"), r)):
            chosen = [c for c in convs if c["model"] == model and c["run"] == run]
            ok = sum(t["manual"].get("verdict") == "pass" for c in chosen for t in c["turns"])
            n = sum(len(c["turns"]) for c in chosen)
            lines += [f"## {model} — {run}", "", f"{len(chosen)} cases / {n} turns; manual pass {ok}/{n}.", ""]
            for c in chosen:
                lines += [f"### {model} · {run} · `{c['case_id']}` ({c['set']}, {c['family']})", "",
                          f"Session `{c['session']}`, started {c['started']}. Case verdict: manual "
                          f"**{c['case_manual']}**, automated "
                          f"{'pass' if c['case_passed_automated'] else 'fail'}.", ""]
                for t in c["turns"]:
                    lines += [f"**Turn {t['turn'] + 1} — user:** {t['user']}", ""]
                    if not t["tool_calls"]:
                        lines += ["- Tool: none"]
                    for i, call in enumerate(t["tool_calls"], start=1):
                        g, r = call["guard"], call["result"]
                        acts = "; ".join(f"{a['action']} {a['argument']}={_j(a['value'])} ({a['reason']})" for a in g.get("actions", []))
                        lines += [f"- Tool {i}: `{call['tool']}`",
                                  f"  - original args: `{_j(call['original_args'])}`",
                                  f"  - guard: **{g['status']}**" + (f" — {acts}" if acts else ""),
                                  f"  - final args: `{_j(call['final_args'])}`",
                                  f"  - result: status `{r.get('status')}`, confidence `{r.get('confidence')}`, totals `{_j(r.get('totals'))}`, "
                                  f"gaps `{_j(r.get('gaps'))}`",
                                  f"  - products: {_products(r.get('products') or [])}"]
                        if r.get("alternatives"):
                            lines += [f"  - alternatives: {_products(r['alternatives'])}"]
                        for k in ("not_applied", "model_resolution", "clarification", "errors"):
                            if r.get(k):
                                lines += [f"  - {k}: `{_j(r[k])}`"]
                    lines += ["", "**Assistant:**", ""] + [f"> {x}" if x else ">" for x in (t["assistant"] or "(empty)").splitlines()]
                    tok = t["tokens"]
                    m = t["manual"]
                    lines += ["", f"- Execution `{t['execution']}`; latency {t['latency_s']} s; model rounds {t['model_rounds']}; "
                              f"tokens in/out {tok.get('promptTokens')}/{tok.get('completionTokens')} (n8n estimate); "
                              f"cost ${t['cost_usd']:.6f}",
                              f"- Automated: **{'pass' if t['automated']['pass'] else 'fail'}**"
                              + (f" — {'; '.join(str(x) for x in t['automated']['reasons'])}" if t["automated"]["reasons"] else ""),
                              f"- Manual: **{m.get('verdict')}**" + (f" — {m['note']}" if m.get("note") else ""), ""]
    return "\n".join(lines) + "\n"


def build(runs: Path, codes_file: Path, log_file: Path, manual_file: Path = None, meta: dict = None) -> dict:
    manual = json.loads(Path(manual_file).read_text())["verdicts"] if manual_file and Path(manual_file).exists() else {}
    turns = collect(Path(runs), load_catalog_codes(codes_file), guard_decisions(Path(log_file).read_text()), manual)
    unknown = set(manual) - {t["key"] for t in turns}
    assert not unknown, f"manual verdicts for unknown turns: {sorted(unknown)[:5]}"
    by_run = {}
    for t in turns:
        by_run.setdefault(t["model"], {}).setdefault(t["run"], []).append(t)
    summary = {m: {run: {"all": metrics(ts), **{s: metrics([t for t in ts if t["set"] == s]) for s in ("frozen", "supplement")
                                                if any(t["set"] == s for t in ts)},
                         "multi_turn": metrics([t for t in ts if t["family"] == "followup"])}
                   for run, ts in runs_.items()} for m, runs_ in by_run.items()}
    meta = {**(meta or {}), "manifest": manifest(), "prices_usd_per_token": {m: list(p) for m, p in MODEL_PRICES.items()}}
    convs = conversations(turns)
    result = {"_meta": meta, "summary": summary,
              "turns": [{k: t[k] for k in ("key", "model", "run", "set", "case_id", "turn", "family", "execution", "latency_s",
                                           "model_rounds", "tokens", "cost_usd")}
                        | {"tools": [c["tool"] for c in t["tool_calls"]], "original_args": [c["original_args"] for c in t["tool_calls"]],
                           "guard": [c["guard"]["status"] for c in t["tool_calls"]],
                           "result_status": [c["result"].get("status") for c in t["tool_calls"]],
                           "auto_pass": t["automated"]["pass"], "auto_reasons": t["automated"]["reasons"], "manual": t["manual"]}
                        for t in turns]}
    text = json.dumps(result, ensure_ascii=False, indent=1)
    audit = json.dumps({"_meta": {**meta, "contents": "all scored dialogues; no model reasoning is recorded"},
                        "conversations": convs}, ensure_ascii=False, indent=1)
    md = render_markdown(convs, meta)
    for blob in (text, audit, md):                                   # nothing secret may reach a committed artifact
        assert not re.search(r"(?i)bearer\s+[A-Za-z0-9]{16}|sk-[A-Za-z0-9_-]{20}|X-N8N-API-KEY\W+\w{16}", blob)
    (RESULTS / "model_bakeoff.json").write_text(text + "\n")
    (RESULTS / "model_bakeoff_conversations.json").write_text(audit + "\n")
    (RESULTS / "model_bakeoff_conversations.md").write_text(md)
    return result


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    extra = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    meta_file = Path(sys.argv[5]) if len(sys.argv) > 5 else None
    res = build(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), extra,
                json.loads(meta_file.read_text()) if meta_file else None)
    for model, runs_ in res["summary"].items():
        for run, s in runs_.items():
            a = s["all"]
            print(model, run, "turns", a["turns"], "auto", a["automated"]["turns_passed"], "manual", a["manual"]["turns_passed"],
                  "latency", a["latency_s"]["median"], "cost", a["cost_usd"]["total"])
