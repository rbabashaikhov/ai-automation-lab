"""Phase 4D Gate 4D.1 offline evaluation: the tool path behind every Agent evaluation case.

    python -m evaluation.run_agent_offline [--out PATH]

No LLM and no n8n. For each case turn that expects a tool, the *expected* arguments (and every
acceptable variant) are executed through the real tool facade against the catalog in the
existing read-only session (``INDEXING_DATABASE_URL``, ``default_transaction_read_only`` verified
before any query). This measures "correct tool + correct arguments -> correct grounded
evidence"; it says nothing about whether the Agent chooses those tools -- that is Gate 4D.2.

Checks per call: the case's ``offline`` assertions (status, codes from the Phase 3D dataset, gap
kinds, feature states), plus contract hygiene on every payload (no internal ids, similarity,
ranking internals or SQL; numeric prices; ``not_listed`` preserved). Also: the injection double.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from consultant.agent_tools import ConsultantTools
from consultant.catalog_repository import CatalogRepository

from .agent_eval import load_cases
from .agent_injection import INJECTION_TEXT, TARGET_MODEL, inject

OUT = Path(__file__).parent / "results" / "agent_offline_4d1.json"
FORBIDDEN_KEYS = frozenset({"similarity", "product_id", "external_id", "source", "ranking_debug", "structured_rank",
                            "final_rank", "preferred_matched", "fit", "embedding", "raw_payload", "description",
                            "sql", "router_rule", "route", "chunk_id", "fact_id", "semantic", "budget"})


def _keys(obj, out: set) -> set:
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


def hygiene(payload: dict) -> list:
    problems = sorted(_keys(payload, set()) & FORBIDDEN_KEYS)
    out = [f"forbidden key {k}" for k in problems]
    for p in [*payload.get("products", []), *payload.get("alternatives", [])]:
        if p.get("price_rub") is not None and not isinstance(p["price_rub"], (int, float)):
            out.append(f"{p.get('model_code')}: non-numeric price")
        if not isinstance(p.get("available"), bool):
            out.append(f"{p.get('model_code')}: availability missing")
    text = json.dumps(payload, ensure_ascii=False)
    if "Цена:" in text or "Наличие:" in text:
        out.append("stale chunk price/availability line present")
    return out


def check(expect: dict, payload: dict) -> list:
    fails = []
    codes = [p["model_code"] for p in payload.get("products", [])]
    kinds = {g["kind"] for g in payload.get("gaps", [])}
    if "status" in expect and payload.get("status") != expect["status"]:
        fails.append(f"status {payload.get('status')} != {expect['status']}")
    for c in expect.get("include_codes", []):
        if c not in codes:
            fails.append(f"missing {c}")
    if "exact_codes" in expect and sorted(codes) != sorted(expect["exact_codes"]):
        fails.append(f"codes {sorted(codes)} != {sorted(expect['exact_codes'])}")
    for k in expect.get("gap_kinds", []):
        if k not in kinds:
            fails.append(f"gap {k} missing")
    if expect.get("gap_kinds_any") and not kinds & set(expect["gap_kinds_any"]):
        fails.append(f"none of gaps {expect['gap_kinds_any']}")
    for code, feats in expect.get("features", {}).items():
        prod = next((p for p in payload.get("products", []) if p["model_code"] == code), {})
        for fid, state in feats.items():
            got = prod.get("features", {}).get(fid)
            got = got if isinstance(got, str) or got is None else got.get("state")
            if got != state:
                fails.append(f"{code}.{fid} = {got} != {state}")
    if "confidence" in expect and payload.get("confidence") != expect["confidence"]:
        fails.append(f"confidence {payload.get('confidence')} != {expect['confidence']}")
    if expect.get("has_alternatives") and not payload.get("alternatives"):
        fails.append("no alternatives")
    return fails


def _summary(payload: dict) -> dict:
    return {"status": payload.get("status"), "confidence": payload.get("confidence"),
            "codes": [p["model_code"] for p in payload.get("products", [])],
            "alternatives": [p["model_code"] for p in payload.get("alternatives", [])],
            "gap_kinds": sorted({g["kind"] for g in payload.get("gaps", [])}),
            "clarification": (payload.get("clarification") or {}).get("reason"),
            "errors": payload.get("errors", []),
            "chars": len(json.dumps(payload, ensure_ascii=False))}


def evaluate(tools: ConsultantTools, cases: list) -> dict:
    rows = []
    for case in cases:
        for i, t in enumerate(case["turns"]):
            if t["tool"] is None or t.get("args") is None:
                continue
            variants = [("expected", t["args"])] + [(f"acceptable_{n}", a)
                                                    for n, a in enumerate(t.get("acceptable_args", []), 1)]
            for label, args in variants:
                payload = tools.call(t["tool"], args)
                row = {"case": case["id"], "turn": i, "family": case["family"], "variant": label, "tool": t["tool"],
                       "args": args, **_summary(payload), "hygiene": hygiene(payload)}
                if label == "expected" and t.get("offline"):
                    row["offline_failures"] = check(t["offline"], payload)
                rows.append(row)
    expected = [r for r in rows if r["variant"] == "expected"]
    asserted = [r for r in expected if "offline_failures" in r]
    return {
        "calls": len(rows),
        "expected_calls": len(expected),
        "asserted": len(asserted),
        "asserted_passed": sum(not r["offline_failures"] for r in asserted),
        "hygiene_violations": sum(len(r["hygiene"]) for r in rows),
        "invalid_arguments": [f"{r['case']}:{r['variant']}" for r in rows if r["status"] == "invalid_arguments"],
        "errors": [f"{r['case']}:{r['variant']}" for r in rows if r["status"] == "error"],
        "max_payload_chars": max((r["chars"] for r in rows), default=0),
        "failures": {f"{r['case']}[{r['turn']}]": r["offline_failures"] for r in asserted if r["offline_failures"]},
        "rows": rows,
    }


def injection_check(tools: ConsultantTools) -> dict:
    real = tools.call("get_tv", {"model": TARGET_MODEL})
    injected = inject(real)
    prod_real = real["products"][0]
    prod_inj = injected["products"][0]
    return {
        "model": TARGET_MODEL,
        "price_unchanged": prod_inj["price_rub"] == prod_real["price_rub"],
        "availability_unchanged": prod_inj["available"] == prod_real["available"],
        "injection_only_inside_catalog_passages": (
            INJECTION_TEXT not in json.dumps({k: v for k, v in prod_inj.items() if k != "catalog_passages"},
                                             ensure_ascii=False)
            and any(p["text"] == INJECTION_TEXT for p in prod_inj["catalog_passages"])),
        "data_notice_present": "never instructions" in injected["data_notice"],
        "live_agent_behaviour": "pending Gate 4D.2 (case adv-injection-in-evidence)",
    }


def main(argv: Optional[list] = None) -> int:
    from .catalog_check import _connect

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    ns = ap.parse_args(argv)
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        ro = cur.fetchone()[0]
    conn.rollback()
    if ro != "on":
        print("refusing: session is not read-only", file=sys.stderr)
        return 2
    repo = CatalogRepository(conn)

    class _RollbackRepo:                       # end each call's read-only transaction
        def __enter__(self):
            return repo

        def __exit__(self, *exc):
            conn.rollback()
            return False

    tools = ConsultantTools(lambda: _RollbackRepo())
    cases = load_cases()
    result = evaluate(tools, cases)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "db_transaction_read_only": ro, "llm_calls": 0, "embeddings_generated": 0,
           "cases": len(cases), "turns": sum(len(c["turns"]) for c in cases),
           "summary": {k: v for k, v in result.items() if k != "rows"},
           "injection_double": injection_check(tools), "rows": result["rows"]}
    conn.close()
    ns.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"summary": out["summary"], "injection_double": out["injection_double"]}, ensure_ascii=False,
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
