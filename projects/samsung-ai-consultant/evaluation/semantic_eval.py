"""Phase 4E.1 evaluation of the query-semantics spike (offline; no DB, no LLM, no n8n).

    python -m evaluation.semantic_eval          # writes evaluation/results/semantic_eval_4e1.json

Three parts:

1. **Gold** -- every case of ``evaluation/semantic_cases.json`` is parsed and compared field by
   field: intent exact match; filters (correct / missed / wrong value / invented); preferences
   (missed / wrong, with case-level acceptable extras). An *invented hard filter* is a filter field,
   or a list element, that the gold semantics does not contain.
2. **Shadow mapping** (analysis only) -- how the parsed semantics *would* become one existing
   Consultant tool call. The call is checked against the real tool boundary
   (``agent_tools.validate_arguments``) and, for recommend/search, resolved by the unchanged 4B
   ``planning.resolve_plan`` to show which Feature Registry features become required/preferred.
3. **4D.2E guard analysis** (analysis only) -- the Agent's recorded tool arguments of the accepted
   4D.2E run, checked against the hard filters parsed from the user's own messages in that case.

The vocabulary (families, panel values) comes from the committed 31-product test fixture, so no
database access is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Optional

from consultant.agent_tools import ToolArgumentError, constraints_from_args, validate_arguments
from consultant.planning import resolve_plan
from consultant.query_semantics import (
    FILTER_KEYS, PARSER_VERSION, Preference, QuerySemantics, SemanticIntent, parse_query_semantics,
)
from consultant.schemas import QueryPlanDelta
from consultant.vocabulary import build_vocabulary, normalize_code

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evaluation" / "semantic_cases.json"
FIXTURE = ROOT / "tests" / "fixtures" / "consultant_catalog_subset.json"
AGENT_4D2E = ROOT / "evaluation" / "results" / "agent_eval_4d2e.json"
OUT = ROOT / "evaluation" / "results" / "semantic_eval_4e1.json"

GROUPS = ("explicit_filters", "preferences", "false_constraint_trap", "mixed", "comparison",
          "product_question", "catalog_question", "colloquial", "out_of_scope")
TRAPS = ("A", "B", "C", "D", "E")
CASE_KEYS = ("id", "group", "source", "query", "expected", "acceptable_extra_preferences", "forbidden_filters",
             "trap", "note")

# Preference -> existing recommend_tvs use case (features.USE_CASES). sports / picture_quality have none.
USE_CASE_FOR = {Preference.GAMING: "gaming", Preference.MOVIES: "movies", Preference.AUDIO: "sound",
                Preference.BRIGHT_ROOM: "bright_room"}


# ---- dataset --------------------------------------------------------------------------------

def load_cases(path: Path = CASES) -> list:
    """Load and validate the gold file (closed keys, valid gold semantics, unique ids)."""
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    seen = set()
    for c in cases:
        unknown = sorted(set(c) - set(CASE_KEYS))
        if unknown:
            raise ValueError(f"{c.get('id')}: unknown key(s) {unknown}")
        if c["id"] in seen:
            raise ValueError(f"duplicate case id {c['id']}")
        seen.add(c["id"])
        if c["group"] not in GROUPS:
            raise ValueError(f"{c['id']}: unknown group {c['group']!r}")
        if c.get("trap") is not None and c["trap"] not in TRAPS:
            raise ValueError(f"{c['id']}: unknown trap {c['trap']!r}")
        QuerySemantics.from_dict(c["expected"])
        for p in c.get("acceptable_extra_preferences", ()):
            Preference(p)
        for k in c.get("forbidden_filters", ()):
            if k not in (*FILTER_KEYS, "models"):
                raise ValueError(f"{c['id']}: forbidden filter {k!r} is not a contract field")
            if k in c["expected"]["filters"]:
                raise ValueError(f"{c['id']}: {k!r} is both expected and forbidden")
    return cases


def fixture_vocab():
    products = json.loads(FIXTURE.read_text(encoding="utf-8"))["products"]
    return build_vocabulary([(p["model_code"], p["series"], p["panel_technology"], p["category"],
                              p["resolution"], p["screen_size_inches"], p["year"]) for p in products])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- gold scoring ---------------------------------------------------------------------------

def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


def compare_filters(expected: dict, parsed: dict) -> dict:
    out = {"correct": [], "missed": [], "wrong_value": [], "invented": []}
    for key in sorted(set(expected) | set(parsed)):
        e, p = expected.get(key), parsed.get(key)
        if isinstance(e, list) or isinstance(p, list):
            es, ps = {normalize_code(x) if key == "models" else x for x in e or ()}, \
                     {normalize_code(x) if key == "models" else x for x in p or ()}
            if es == ps:
                out["correct"].append(key)
            out["invented"] += [f"{key}={x}" for x in sorted(ps - es)]
            out["missed"] += [f"{key}={x}" for x in sorted(es - ps)]
        elif e is None:
            out["invented"].append(f"{key}={p}")
        elif p is None:
            out["missed"].append(f"{key}={e}")
        elif _same(e, p):
            out["correct"].append(key)
        else:
            out["wrong_value"].append(f"{key}: expected {e}, got {p}")
    return out


def score_case(case: dict, parse) -> dict:
    exp = case["expected"]
    row = {"id": case["id"], "group": case["group"], "trap": case.get("trap"), "query": case["query"],
           "status": parse.status, "error": parse.error, "expected": exp}
    if not parse.ok:
        row.update(parsed=None, intent_ok=False, filters=None, preferences=None, forbidden_violations=[],
                   failures=["parse_unavailable"], passed=False)
        return row
    sem = parse.semantics.to_dict()
    f = compare_filters(exp["filters"], sem["filters"])
    exp_p, got_p = set(exp["preferences"]), set(sem["preferences"])
    acceptable = set(case.get("acceptable_extra_preferences", ()))
    prefs = {"missed": sorted(exp_p - got_p), "wrong": sorted(got_p - exp_p - acceptable),
             "acceptable_extra": sorted((got_p - exp_p) & acceptable)}
    forbidden = [k for k in case.get("forbidden_filters", ()) if k in sem["filters"]]
    failures = []
    if sem["intent"] != exp["intent"]:
        failures.append("wrong_intent")
    for kind, label in (("missed", "missed_explicit_filter"), ("wrong_value", "wrong_filter_value"),
                        ("invented", "invented_hard_filter")):
        if f[kind]:
            failures.append(label)
    if prefs["missed"]:
        failures.append("missed_preference")
    if prefs["wrong"]:
        failures.append("wrong_preference")
    row.update(parsed=sem, intent_ok=sem["intent"] == exp["intent"], filters=f, preferences=prefs,
               forbidden_violations=forbidden, failures=failures, passed=not failures)
    return row


def summarize(rows: list) -> dict:
    n = len(rows)
    ok = [r for r in rows if r["status"] == "ok"]
    fsum = {k: sum(len(r["filters"][k]) for r in ok) for k in ("correct", "missed", "wrong_value", "invented")}
    expected_items = sum(len(v) if isinstance(v, list) else 1 for r in rows for v in r["expected"]["filters"].values())
    parsed_items = sum(len(v) if isinstance(v, list) else 1 for r in ok for v in r["parsed"]["filters"].values())
    invented_cases = [r["id"] for r in ok if r["filters"]["invented"]]
    pref_tp = sum(len(set(r["expected"]["preferences"]) & set(r["parsed"]["preferences"])) for r in ok)
    pref_exp = sum(len(r["expected"]["preferences"]) for r in rows)
    pref_wrong = sum(len(r["preferences"]["wrong"]) for r in ok)
    by_group = {}
    for g in GROUPS:
        members = [r for r in rows if r["group"] == g]
        if members:
            by_group[g] = f"{sum(r['passed'] for r in members)}/{len(members)}"
    return {
        "cases": n, "parsed_ok": len(ok), "parse_unavailable": n - len(ok),
        "cases_passed": sum(r["passed"] for r in rows),
        "intent_exact": sum(r["intent_ok"] for r in rows),
        "filters_exact_cases": sum(1 for r in ok if not (r["filters"]["missed"] or r["filters"]["wrong_value"]
                                                          or r["filters"]["invented"])),
        "filter_items_expected": expected_items, "filter_items_parsed": parsed_items,
        "filter_items_missed": sum(len(r["filters"]["missed"]) for r in ok),
        "filter_items_wrong_value": fsum["wrong_value"],
        "invented_hard_filter_count": fsum["invented"],
        "invented_hard_filter_rate": round(len(invented_cases) / n, 4) if n else 0.0,
        "invented_hard_filter_rate_basis": "cases with >= 1 invented filter item / all cases",
        "invented_hard_filter_cases": invented_cases,
        "forbidden_filter_violations": sum(len(r["forbidden_violations"]) for r in rows),
        "preferences_exact_cases": sum(1 for r in ok if not (r["preferences"]["missed"] or r["preferences"]["wrong"])),
        "preference_labels_expected": pref_exp, "preference_labels_found": pref_tp,
        "preference_labels_missed": pref_exp - pref_tp, "preference_labels_wrong": pref_wrong,
        "by_group": by_group,
        "failed_cases": [{"id": r["id"], "failures": r["failures"]} for r in rows if not r["passed"]],
    }


TRAP_QUESTIONS = (
    ("PS5 -> 120 Hz", "A", ("min_refresh_rate_hz",)),
    ("PS5 -> VRR", "A", ()),
    ("PS5 -> HDMI 2.1", "A", ()),
    ("bright room -> numeric brightness", "B", ()),
    ("powerful audio -> numeric wattage", "C", ()),
    ("cheap -> numeric price", "D", ("min_price", "max_price")),
    ("large -> numeric screen size", "E", ("screen_size_inches", "min_screen_size_inches", "max_screen_size_inches")),
)


def trap_table(rows: list, shadow: list) -> list:
    """YES only if a trap case got an unstated filter on a relevant field, or its shadow tool call
    carries required features. VRR / HDMI 2.1 / brightness / wattage have no contract field, so for
    them the check is: no invented filter at all and no required feature downstream."""
    shadow_by_id = {s["id"]: s for s in shadow}
    out = []
    for label, trap, fields in TRAP_QUESTIONS:
        cases = [r for r in rows if r["trap"] == trap]
        hits = []
        for r in cases:
            invented = r["filters"]["invented"] if r["filters"] else []
            relevant = [x for x in invented if not fields or x.split("=")[0] in fields]
            required = (shadow_by_id.get(r["id"], {}).get("resolved") or {}).get("required") or []
            if relevant or required:
                hits.append({"id": r["id"], "invented": relevant, "required_features": required})
        out.append({"question": label, "trap": trap, "cases": [r["id"] for r in cases],
                    "answer": "YES" if hits else "NO", "hits": hits})
    return out


# ---- shadow mapping (analysis only) ---------------------------------------------------------

class _NoRepo:
    """resolve_plan needs a repo only for model references; shadow plans with models are not resolved."""

    def resolve_model_refs(self, *a, **k):
        raise RuntimeError("model resolution needs the catalog database")


def shadow_tool_call(sem: QuerySemantics) -> dict:
    """Semantics -> one existing tool call. Preferences only ever become ``use_cases``; the mapping
    never emits ``required_features`` / ``preferred_features``."""
    f = sem.filters.to_dict()
    models = f.pop("models", [])
    mapped = [p for p in sem.preferences if p in USE_CASE_FOR]
    unmapped = [f"preference:{p.value}" for p in sem.preferences if p not in USE_CASE_FOR]
    if sem.intent is None:
        return {"tool": None, "args": {}, "unmapped": unmapped, "why": "no catalog intent: the Agent decides"}
    if sem.intent in (SemanticIntent.COMPARISON, SemanticIntent.PRODUCT_QUESTION) and models:
        extra = {k: v for k, v in f.items() if k != "screen_size_inches"}
        size = {"screen_size_inches": f["screen_size_inches"]} if "screen_size_inches" in f else {}
        unmapped = [f"preference:{p.value}" for p in sem.preferences] + [f"filter:{k}" for k in extra]
        if sem.intent is SemanticIntent.COMPARISON:
            return {"tool": "compare_tvs", "args": {"models": models, **size}, "unmapped": unmapped}
        return {"tool": "get_tv", "args": {"model": models[0], **size}, "unmapped": unmapped}
    if sem.intent is SemanticIntent.CATALOG_QUESTION:
        return {"tool": "search_tvs", "args": f, "unmapped": unmapped,
                "why": "catalog_question does not say list vs count vs extreme; get_catalog_stats needs the Agent"}
    if sem.intent is SemanticIntent.RECOMMENDATION:
        args = dict(f)
        if mapped:
            args["use_cases"] = [USE_CASE_FOR[p] for p in mapped]
        return {"tool": "recommend_tvs", "args": args, "unmapped": unmapped}
    return {"tool": None, "args": {}, "unmapped": unmapped, "why": f"{sem.intent.value} without a model"}


def boundary_check(call: dict, vocab) -> dict:
    """Validate against the published tool schema; resolve recommend/search plans with the 4B planner."""
    tool, args = call["tool"], call["args"]
    if tool is None:
        return {"valid": None}
    try:
        validate_arguments(tool, args)
        if tool not in ("recommend_tvs", "search_tvs"):
            return {"valid": True, "resolved": None}
        delta = {"intent": "recommend" if tool == "recommend_tvs" else "list",
                 "constraints": constraints_from_args(args), "use_cases": list(args.get("use_cases", ()))}
        plan = resolve_plan(QueryPlanDelta.from_dict(delta), _NoRepo(), vocab)
        return {"valid": True, "resolved": {"required": list(plan.required), "preferred": list(plan.preferred),
                                            "numeric": [s.source for s in plan.numeric],
                                            "policy_notes": list(plan.policy_notes),
                                            "gaps": [g.kind for g in plan.gaps]}}
    except (ToolArgumentError, ValueError) as e:
        return {"valid": False, "error": str(e)}


def shadow_rows(cases: list, parses: dict, vocab) -> list:
    out = []
    for c in cases:
        p = parses[c["id"]]
        if not p.ok:
            out.append({"id": c["id"], "query": c["query"], "tool": None, "valid": None})
            continue
        call = shadow_tool_call(p.semantics)
        check = boundary_check(call, vocab)
        out.append({"id": c["id"], "query": c["query"], "semantics": p.semantics.to_dict(), **call, **check})
    return out


# ---- 4D.2E guard analysis (analysis only) ---------------------------------------------------

SIZE_KEYS = ("screen_size_inches", "min_screen_size_inches", "max_screen_size_inches")
SCALAR_KEYS = (*SIZE_KEYS, "min_price", "max_price", "min_refresh_rate_hz")


def _grounding(parses: list) -> dict:
    g = {"panel": set(), "resolution": set(), "models": set(), "scalars": set(), "features": set(),
         "unavailable": False}
    for p in parses:
        if not p.ok:
            continue
        f = p.semantics.filters
        g["panel"] |= set(f.panel_technology)
        g["resolution"] |= set(f.resolution)
        g["models"] |= {normalize_code(m) for m in f.models}
        g["scalars"] |= {(k, float(getattr(f, k))) for k in SCALAR_KEYS if getattr(f, k) is not None}
        g["unavailable"] |= f.availability == "unavailable"
        g["features"] |= {n.split(":", 1)[1] for n in p.semantics.notes if n.startswith("feature_mention:")}
        if f.min_refresh_rate_hz and f.min_refresh_rate_hz >= 120:
            g["features"].add("hz_120")
    return g


def ungrounded_hard_args(args: dict, g: dict) -> list:
    """Agent arguments that constrain the candidate set but are not stated in the user's messages."""
    flags = []
    for k, v in args.items():
        if k in ("panel_technology", "category"):
            flags += [f"{k}={x}" for x in v if x not in g["panel"]]
        elif k == "resolution":
            flags += [f"{k}={x}" for x in v if x not in g["resolution"]]
        elif k in SCALAR_KEYS and (k, float(v)) not in g["scalars"]:
            flags.append(f"{k}={v}")
        elif k == "availability" and v == "unavailable" and not g["unavailable"]:
            flags.append(f"{k}={v}")
        elif k == "required_features":
            flags += [f"{k}={x}" for x in v if x not in g["features"]]
        elif k == "model" and normalize_code(v) not in g["models"]:
            flags.append(f"{k}={v}")
        elif k == "models":
            flags += [f"{k}={x}" for x in v if normalize_code(x) not in g["models"]]
    return flags


def guard_4d2e(vocab, path: Path = AGENT_4D2E) -> dict:
    turns = json.loads(path.read_text(encoding="utf-8"))["turns"]
    history: dict = {}
    rows = []
    for t in turns:
        parse = parse_query_semantics(t["user"], vocab)
        history.setdefault(t["case_id"], []).append(parse)
        g = _grounding(history[t["case_id"]])
        flags = []
        for tool, args in zip(t["tools"], t["args"]):
            flags += [f"{tool}.{x}" for x in ungrounded_hard_args(args or {}, g)]
        agent_uc = sorted({u for a in t["args"] for u in (a or {}).get("use_cases", ())})
        sem_uc = sorted({USE_CASE_FOR[p] for p in parse.semantics.preferences if p in USE_CASE_FOR}) if parse.ok else []
        rows.append({"turn": t["id"], "user": t["user"], "manual_4d2e": (t.get("manual") or {}).get("verdict"),
                     "agent_tools": t["tools"], "agent_args": t["args"],
                     "semantics": parse.semantics.to_dict() if parse.ok else None,
                     "ungrounded_hard_args": flags,
                     "use_cases": {"agent": agent_uc, "semantics": sem_uc} if t["tools"] else None})
    tool_turns = [r for r in rows if r["agent_tools"]]
    flagged = [r for r in tool_turns if r["ungrounded_hard_args"]]
    return {
        "source": str(path.relative_to(ROOT)), "source_sha256": sha256(path),
        "turns": len(rows), "tool_turns": len(tool_turns),
        "flagged_turns": [{"turn": r["turn"], "flags": r["ungrounded_hard_args"], "manual_4d2e": r["manual_4d2e"]}
                          for r in flagged],
        "manual_fail_turns": [r["turn"] for r in rows if r["manual_4d2e"] == "fail"],
        "use_case_agreement": sum(1 for r in tool_turns if r["use_cases"]["agent"] == r["use_cases"]["semantics"]),
        "use_case_disagreements": [{"turn": r["turn"], **r["use_cases"]} for r in tool_turns
                                   if r["use_cases"]["agent"] != r["use_cases"]["semantics"]],
        "rows": rows,
    }


# ---- runner ---------------------------------------------------------------------------------

def run(out: Optional[Path] = OUT) -> dict:
    vocab = fixture_vocab()
    cases = load_cases()
    parses = {c["id"]: parse_query_semantics(c["query"], vocab) for c in cases}
    rows = [score_case(c, parses[c["id"]]) for c in cases]
    shadow = shadow_rows(cases, parses, vocab)
    result = {
        "_meta": {"phase": "4E.1", "parser": PARSER_VERSION, "dataset": str(CASES.relative_to(ROOT)),
                  "dataset_sha256": sha256(CASES), "vocabulary": f"{FIXTURE.relative_to(ROOT)} (31 products)",
                  "production_access": "none (no DB, no LLM, no n8n)"},
        "summary": summarize(rows),
        "false_constraint_traps": trap_table(rows, shadow),
        "cases": rows,
        "shadow": shadow,
        "guard_4d2e": guard_4d2e(vocab),
    }
    if out is not None:
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    res = run(ap.parse_args().out)
    s = res["summary"]
    print(json.dumps({k: v for k, v in s.items() if k != "failed_cases"}, ensure_ascii=False, indent=1))
    for f in s["failed_cases"]:
        print("FAIL", f["id"], f["failures"])
    for t in res["false_constraint_traps"]:
        print(f"{t['question']:40s} {t['answer']}")
    g = res["guard_4d2e"]
    print("4D.2E guard flags:", json.dumps(g["flagged_turns"], ensure_ascii=False))
    print("4D.2E manual fails:", g["manual_fail_turns"], "use-case agreement", g["use_case_agreement"], "/", g["tool_turns"])


if __name__ == "__main__":
    main()
