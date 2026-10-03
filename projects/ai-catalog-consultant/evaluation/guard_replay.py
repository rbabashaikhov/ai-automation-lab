"""Gate 4E.2 offline replay of the semantic guard (no DB, no LLM, no n8n).

    python -m evaluation.guard_replay      # writes evaluation/results/guard_replay_4e2.json

1. **Frozen 4D.2E replay** -- every recorded Agent tool call of the accepted 4D.2E run is passed
   through ``semantic_guard.guard_tool_arguments`` with the user's messages of that case so far.
   The final arguments are re-validated at the real tool boundary. Registered expectation (before
   the first run): exactly the two known inventions change; every other call is unchanged.
2. **Supplementary cases** (``evaluation/guard_cases.json``) -- explicit requirements, carried
   constraints, thin_wall / compact, model-code size, and invented constraints.
3. **No-invention property** -- over every call: guarded hard constraints are a subset of the
   Agent's, with identical values; only ``use_cases`` may grow.
4. **Counterfactual Core result** -- for a modified call whose final arguments equal a call the
   real Core already executed in the Gate 4D.1 offline run, that recorded outcome is shown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from consultant.agent_tools import ToolArgumentError, constraints_from_args, validate_arguments
from consultant.semantic_guard import GUARD_VERSION, guard_tool_arguments
from consultant.query_semantics import parse_query_semantics

ROOT = Path(__file__).resolve().parents[1]
AGENT_4D2E = ROOT / "evaluation" / "results" / "agent_eval_4d2e.json"
OFFLINE_4D1 = ROOT / "evaluation" / "results" / "agent_offline_4d1.json"
GUARD_CASES = ROOT / "evaluation" / "guard_cases.json"
OUT = ROOT / "evaluation" / "results" / "guard_replay_4e2.json"

# Registered before the first replay: the two known 4D.2E Agent inventions and their correction.
EXPECTED_MODIFICATIONS = {
    "rec-gaming[0]": [["removed", "required_features", "hdmi_2_1", "required_feature_not_mentioned"]],
    "followup-oled65-spike[2]": [["removed", "max_price", 150000, "unsupported_price_value"]],
}
SOFT_KEYS = {"use_cases"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def boundary(tool: str, args: dict) -> str:
    try:
        validate_arguments(tool, args)
        if tool in ("search_tvs", "recommend_tvs", "get_catalog_stats"):
            constraints_from_args(args)
        return "valid"
    except ToolArgumentError as e:
        return "invalid: " + "; ".join(e.errors)


def invented(original: dict, final: dict) -> list:
    """Anything the guard added or altered other than growing use_cases."""
    out = [f"added {k}" for k in final if k not in original and k not in SOFT_KEYS]
    for k, v in final.items():
        if k in SOFT_KEYS:
            if not set(original.get(k, ())) <= set(v):
                out.append(f"dropped {k} value")
        elif k in original and v != original[k] and not (isinstance(v, list) and set(v) <= set(original[k])):
            out.append(f"altered {k}")
    return out


def _offline_index() -> dict:
    rows = json.loads(OFFLINE_4D1.read_text(encoding="utf-8"))["rows"]
    return {(r["tool"], json.dumps(r["args"], sort_keys=True)): r for r in rows if r.get("variant") == "expected"}


def replay_4d2e() -> dict:
    turns = json.loads(AGENT_4D2E.read_text(encoding="utf-8"))["turns"]
    offline = _offline_index()
    history: dict = {}
    rows = []
    for t in turns:
        conv = history.setdefault(t["case_id"], [])
        conv.append(t["user"])
        for tool, args in zip(t["tools"], t["args"]):
            args = args or {}
            g = guard_tool_arguments(tool, args, list(conv))
            final = g.arguments
            parse = parse_query_semantics(t["user"])
            row = {"turn": t["id"], "case_id": t["case_id"], "user": t["user"], "conversation_messages": len(conv),
                   "manual_4d2e": (t.get("manual") or {}).get("verdict"), "tool": tool, "agent_args": args,
                   "semantics": parse.semantics.to_dict() if parse.ok else None,
                   "guard_status": g.status, "guard_detail": g.detail,
                   "actions": [[a.action, a.argument, a.value, a.reason] for a in g.actions],
                   "final_args": final, "validation": boundary(tool, final), "invented": invented(args, final)}
            rec = offline.get((tool, json.dumps(final, sort_keys=True)))
            if g.status == "modified" and rec:
                row["counterfactual_core_4d1"] = {"case": rec["case"], "turn": rec["turn"], "status": rec["status"],
                                                  "codes": rec.get("codes"), "confidence": rec.get("confidence")}
            rows.append(row)
    modified = {r["turn"]: r["actions"] for r in rows if r["guard_status"] == "modified"}
    unexpected = {k: v for k, v in modified.items() if EXPECTED_MODIFICATIONS.get(k) != v}
    missing = [k for k in EXPECTED_MODIFICATIONS if k not in modified]
    return {
        "source": str(AGENT_4D2E.relative_to(ROOT)), "source_sha256": _sha(AGENT_4D2E),
        "tool_calls": len(rows), "unchanged": sum(r["guard_status"] == "unchanged" for r in rows),
        "modified": len(modified), "other_status": sorted({r["guard_status"] for r in rows} - {"unchanged", "modified"}),
        "expected_modifications": EXPECTED_MODIFICATIONS, "unexpected_modifications": unexpected,
        "missing_expected_modifications": missing,
        "validation_failures": [r["turn"] for r in rows if r["validation"] != "valid"],
        "invented_by_guard": [{"turn": r["turn"], "invented": r["invented"]} for r in rows if r["invented"]],
        "passed": not unexpected and not missing and all(r["validation"] == "valid" and not r["invented"] for r in rows),
        "rows": rows,
    }


def replay_cases(path: Path = GUARD_CASES) -> dict:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("duplicate guard case id")
    rows = []
    for c in cases:
        g = guard_tool_arguments(c["tool"], c["args"], c["conversation"])
        ok = g.status == c["expected_status"] and g.arguments == c["expected_args"]
        rows.append({"id": c["id"], "conversation": c["conversation"], "tool": c["tool"], "agent_args": c["args"],
                     "guard_status": g.status, "actions": [[a.action, a.argument, a.value, a.reason] for a in g.actions],
                     "final_args": g.arguments, "expected_args": c["expected_args"],
                     "validation": boundary(c["tool"], g.arguments), "invented": invented(c["args"], g.arguments),
                     "passed": ok})
    return {"source": str(path.relative_to(ROOT)), "source_sha256": _sha(path), "cases": len(rows),
            "passed": sum(r["passed"] for r in rows), "failed": [r["id"] for r in rows if not r["passed"]],
            "invented_by_guard": [r["id"] for r in rows if r["invented"]],
            "validation_failures": [r["id"] for r in rows if r["validation"] != "valid"], "rows": rows}


def run(out=OUT) -> dict:
    result = {"_meta": {"phase": "4E.2", "guard": GUARD_VERSION,
                        "production_access": "none (recorded 4D.2E / 4D.1 artifacts only; no DB, LLM or n8n)"},
              "replay_4d2e": replay_4d2e(), "supplementary": replay_cases()}
    if out is not None:
        Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    res = run(ap.parse_args().out)
    r, s = res["replay_4d2e"], res["supplementary"]
    print(json.dumps({k: v for k, v in r.items() if k != "rows"}, ensure_ascii=False, indent=1))
    for row in r["rows"]:
        if row["guard_status"] != "unchanged":
            print("MODIFIED", row["turn"], row["actions"], "->", row["final_args"], row["validation"],
                  row.get("counterfactual_core_4d1"))
    print(f"supplementary {s['passed']}/{s['cases']}, failed {s['failed']}, invented {s['invented_by_guard']}, "
          f"invalid {s['validation_failures']}")


if __name__ == "__main__":
    main()
