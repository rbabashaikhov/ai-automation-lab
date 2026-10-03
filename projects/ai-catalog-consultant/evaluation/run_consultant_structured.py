"""Phase 4B evaluation: gold plan -> router -> structured retrieval -> features -> ranking.

    python -m evaluation.run_consultant_structured [--out PATH]

Read-only (the same read-only session as ``catalog_check``). No LLM, no embeddings, no OpenAI.
Scores the 21 unchanged Phase 3D cases with the unchanged ``evaluation.scoring`` gates, using
the hand-authored plans in ``consultant_gold_plans.json``, and puts every case next to its
Phase 3D.2 baseline (``results/baseline_3d2.json``).

Mechanism label: structured runs are reported as ``"sql"`` (lookup / list / aggregate) or
``"structured"`` (constraint-first ranking) -- neither is a vector mechanism, so the aggregate
"no vector retrieval" gate is meaningful. Rank metrics for recommend cases are computed over the
full ranking (qualified, then unknown); shortlist membership is reported separately.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from consultant.catalog_repository import CatalogRepository
from consultant.retrieval import run
from consultant.schemas import QueryPlanDelta, Route

from .dataset import load_dataset
from .scoring import CaseRun, render_summary, score_case, summarize

GOLD = Path(__file__).parent / "consultant_gold_plans.json"
BASELINE = Path(__file__).parent / "results" / "baseline_3d2.json"
OUT = Path(__file__).parent / "results" / "structured_core_4b.json"
TOP_N = 5


def load_gold_plans(path: Path = GOLD) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {cid: QueryPlanDelta.from_dict(raw) for cid, raw in data["plans"].items()}


def _codes(rows) -> list:
    return [p.model_code for p in rows]


def evaluate_case(case, delta: QueryPlanDelta, repo: CatalogRepository, vocab) -> tuple:
    """Returns (CaseRun | None, record dict)."""
    plan, rp, res = run(delta, repo, vocab)
    rec = {"id": case.id, "family": case.family, "query": case.query, "route": rp.route.value,
           "router_rule": rp.rule, "executed": res.executed, "policy_notes": list(plan.policy_notes),
           "gaps": [[g.kind, g.detail, list(g.model_codes)] for g in res.gaps],
           "excluded_unavailable": res.excluded_unavailable_count,
           "excluded_out_of_scope": res.excluded_out_of_scope_count, "expected": list(case.relevant_product_ids)}
    if not res.executed:
        rec["reason"] = f"{rp.route.value} is a semantic route (Phase 4C); not executed in 4B"
        return None, rec
    if res.ranking is None:
        codes = _codes(res.products)
        rec["actual"] = codes
        return CaseRun(case.id, "sql", products=codes), rec
    ranked = res.ranking.ranked_codes()
    admitted = _codes(res.candidates)
    explain = {c.product.model_code: c.explain() for c in (*res.ranking.qualified, *res.ranking.unknown)}
    rec.update(
        candidates={"count": len(admitted), "admitted": admitted},
        qualified=len(res.ranking.qualified), unknown=len(res.ranking.unknown), rejected=len(res.ranking.rejected),
        top5=[explain[c] for c in ranked[:TOP_N]],
        expected_product_ranks={p: (ranked.index(p) + 1 if p in ranked else None) for p in case.relevant_product_ids},
        expected_explain={p: explain.get(p) for p in case.relevant_product_ids},
        shortlist={"policy": res.shortlist.policy, "codes": [c.product.model_code for c in res.shortlist.items],
                   "tiers": res.shortlist.tiers, "notes": list(res.shortlist.notes)},
        expected_in_shortlist=[p for p in case.relevant_product_ids
                               if p in {c.product.model_code for c in res.shortlist.items}],
    )
    return CaseRun(case.id, "structured", products=ranked, filter_admitted=admitted), rec


def evaluate(repo: CatalogRepository, cases, plans: dict) -> tuple:
    vocab = repo.vocabulary()
    records, results = [], []
    for case in cases:
        run_, rec = evaluate_case(case, plans[case.id], repo, vocab)
        res = score_case(case, run_)
        rec.update(status=res.status, metrics=res.metrics, checks=[list(c) for c in res.checks])
        records.append(rec)
        results.append(res)
    return records, results


def attach_baseline(records: list, baseline_path: Path = BASELINE) -> None:
    base = {c["id"]: c for c in json.loads(Path(baseline_path).read_text(encoding="utf-8"))["cases"]}
    for r in records:
        b = base.get(r["id"])
        if b:
            r["baseline_3d2"] = {"mechanism": b.get("mechanism"), "status": b.get("status"),
                                 "expected_product_ranks": b.get("expected_product_ranks"),
                                 "mrr": (b.get("metrics") or {}).get("mrr")}


def former_vector_primary_summary(records: list) -> dict:
    """Macro Hit@K / MRR for the six Phase 3D vector_primary cases, 4B vs 3D.2 baseline."""
    rows = [r for r in records if r["family"] == "vector_primary"]

    def agg(get):
        vals = [get(r) for r in rows]
        return {f"hit@{k}": round(sum(v <= k for v in vals if v) / len(rows), 4) for k in (1, 3, 5)} | {
            "mrr": round(sum(1 / v for v in vals if v) / len(rows), 4)}

    def best(ranks):
        known = [v for v in (ranks or {}).values() if v]
        return min(known) if known else None

    return {"cases": len(rows),
            "structured_4b": agg(lambda r: best(r.get("expected_product_ranks"))),
            "vector_3d2": agg(lambda r: best(r.get("baseline_3d2", {}).get("expected_product_ranks")))}


def main(argv: Optional[list] = None) -> int:
    from .catalog_check import _connect

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    cases, plans = load_dataset(), load_gold_plans()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            ro = cur.fetchone()[0]
        if ro != "on":
            raise SystemExit("refusing to run: session is not read-only")
        records, results = evaluate(CatalogRepository(conn), cases, plans)
    finally:
        conn.rollback()
        conn.close()
    attach_baseline(records)
    summary = summarize(results)
    vp = former_vector_primary_summary(records)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "db_transaction_read_only": ro, "openai_requests": 0, "embeddings_generated": 0,
           "routes": {r["id"]: r["route"] for r in records},
           "semantic_routes_on_aggregate_cases": [r["id"] for r in records if r["family"] == "aggregate_not_retrieval"
                                                  and r["route"] in (Route.PRODUCT_SCOPED_SEMANTIC.value,
                                                                     Route.SEMANTIC_FALLBACK.value)],
           "former_vector_primary": vp, "cases": records, "summary": summary}
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for r, res in zip(records, results):
        print(res.render())
        print(f"    route={r['route']} ({r['router_rule']})")
    print(render_summary(summary))
    print("\nformer vector_primary (6 cases):", json.dumps(vp, ensure_ascii=False))
    print(f"results written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
