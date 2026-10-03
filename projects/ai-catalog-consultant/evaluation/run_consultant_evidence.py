"""Phase 4C evaluation: evidence coverage and the contribution of semantic retrieval.

    python -m evaluation.run_consultant_evidence [--out PATH]

Read-only session only (verified). Vectors: ONLY the Phase 3D cache
``results/query_embeddings.json`` -- no OpenAI, no new embeddings; texts without a cached vector
are reported as "semantic unavailable". Sections:

A. Phase 4B baseline re-run unchanged (must equal ``results/structured_core_4b.json``).
B. Evidence coverage for the 21 unchanged gold plans (as authored: mapped needs -> section
   lookup, 0 embeddings; the one unmapped plan -> global fallback with its cached vector).
C. Experimental arm "need text as free_text_need": gold plan + ``free_text_need = case query``
   for CONSTRAINT_FIRST cases with a cached vector -> candidate-scoped retrieval and the
   fit-tie-only semantic tie-break. Gold plans are not edited; this arm is labelled as such.
D. Product-scoped section location: for cached need queries with a registry-derived section,
   rank of that section among each product's own chunks.
E. Product-scoped long-tail probe cases (deterministic lexical probe; truth by independent
   hand-written patterns) -> verified present / verified absent / unverified / false absence.
F. Bright-room and movies corpus analysis.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Optional

from consultant.catalog_repository import CatalogRepository
from consultant.evidence import build_evidence, serialize_for_llm
from consultant.features import FEATURES
from consultant.planning import resolve_plan
from consultant.retrieval import execute, run
from consultant.router import route
from consultant.schemas import Filters, Intent, ModelRef, QueryPlanDelta, RefKind
from consultant.semantic import CachedQueryEmbeddings

from . import metrics
from .dataset import load_dataset
from .run_consultant_structured import evaluate, former_vector_primary_summary, load_gold_plans, attach_baseline

CACHE = Path(__file__).parent / "results" / "query_embeddings.json"
PHASE_4B = Path(__file__).parent / "results" / "structured_core_4b.json"
OUT = Path(__file__).parent / "results" / "evidence_semantic_4c.json"

# Product-scoped long-tail cases. truth_pattern is an independent, hand-written ILIKE pattern for
# how the catalog would state the attribute (not derived from the probe's term extraction).
PROBE_CASES = (
    ("MRE115MR95FXRU", "AirPlay", "%AirPlay%"),
    ("QE65S95HAUXPY", "AirPlay", "%AirPlay%"),
    ("QE65S95HAUXPY", "Bixby", "%Bixby%"),
    ("QE65S95HAUXPY", "голосовой помощник", "Способы управления:%Голос%"),
    ("MNA114MS1CCXRU", "HDMI 2.1", "Версия HDMI: 2.1%"),
    ("QE65S95HAUXPY", "HDMI 2.1", "Версия HDMI: 2.1%"),
    ("UE27LSM7FAXXPY", "USB-C", "USB-C: %"),
    ("QE65S95HAUXPY", "USB-C", "USB-C: %"),
    ("MRE100R85HUXPY", "Wi-Fi 6E", "Стандарты Wi-Fi:%6E%"),
    ("QE43QN70HAUXPY", "Wi-Fi 6E", "Стандарты Wi-Fi:%6E%"),
)
# Cached need query -> (registry-derived section, line that carries the signal)
PRODUCT_SCOPED_QUERIES = (
    ("хочу хороший звук без отдельного саундбара", "audio", "Мощность звука"),
    ("телевизор для игровой приставки", "gaming", None),
    ("тонкий телевизор на стену", "physical_design", "Размер без подставки"),
    ("телевизор с поддержкой VRR (Variable Refresh Rate)", "display", "Variable Refresh Rate"),
    ("для светлой комнаты", "display", "Антибликовое покрытие"),
)
CORPUS_TERMS = {
    "bright_room": ("ярк", "brightness", "nits", "кд/м", "Антибликов", "Anti Reflection", "Supreme UHD Dimming",
                    "Mini LED", "Quantum Mini LED", "Micro Dimming", "Self-illuminating"),
    "movies": ("Filmmaker", "Кино", "Movie", "Dolby Atmos", "Dolby Vision", "IMAX", "HDR10+", "Q-Symphony",
               "360 Audio", "Perceptional Color Mapping", "Мощность звука, Вт: 70"),
}
LABELS = {"bright_room": ("semantic-bright-room", "для светлой комнаты"),
          "movies": ("semantic-movies", "телевизор для фильмов")}


def gold_sections(plan) -> set:
    return {FEATURES[f].section for f in (*plan.required, *plan.preferred)} | {
        FEATURES[s.source].section for s in plan.numeric if s.source in FEATURES}


def _rank_metrics(ranked, relevant) -> dict:
    return {"hit@1": metrics.hit_at_k(ranked, relevant, 1), "hit@3": metrics.hit_at_k(ranked, relevant, 3),
            "hit@5": metrics.hit_at_k(ranked, relevant, 5), "mrr": round(metrics.reciprocal_rank(ranked, relevant), 4),
            "first_relevant_rank": metrics.first_relevant_rank(ranked, relevant)}


def evidence_coverage(repo, cases, plans, emb) -> list:
    out = []
    for case in cases:
        plan, rp, res = run(plans[case.id], repo)
        before = emb.hits + emb.misses
        b = build_evidence(plan, rp, res, repo, emb)
        codes = [p.model_code for p in b.products]
        wanted_features = set(plan.required) | set(plan.preferred) | set(plan.attributes_asked)
        shown_features = {f.feature_id for p in b.products for f in p.features}
        out.append({
            "id": case.id, "family": case.family, "route": b.route, "confidence": b.retrieval_confidence,
            "confidence_reasons": list(b.confidence_reasons), "products": len(b.products),
            "alternatives": len(b.alternatives), "labelled_in_evidence": [c for c in case.relevant_product_ids if c in codes],
            "labelled_total": len(case.relevant_product_ids),
            "features_represented": sorted(wanted_features & shown_features),
            "features_missing": sorted(wanted_features - shown_features),
            "gaps": sorted({g.kind for g in b.gaps}),
            "passages": sum(len(p.passages) for p in b.products),
            "passage_paths": sorted({x.retrieval for p in b.products for x in p.passages}),
            "passage_sections": sorted({x.section for p in b.products for x in p.passages}),
            "embedding_lookups": emb.hits + emb.misses - before, "semantic_path": b.semantic["path"],
            "approx_tokens": b.budget["approx_tokens"], "trimmed_passages": b.budget["trimmed_passages"],
            "llm_payload_has_similarity": "similarity" in serialize_for_llm(b),
            "stale_lines_stripped": sum(x.stripped_lines for p in b.products for x in p.passages),
        })
    return out


def experimental_arm(repo, cases, plans, emb) -> list:
    out = []
    for case in cases:
        if case.query not in emb._vectors:
            continue
        base_plan = resolve_plan(plans[case.id], repo)
        if route(base_plan).route.value != "CONSTRAINT_FIRST":
            continue
        plan = resolve_plan(replace(plans[case.id], free_text_need=case.query), repo)
        rp = route(plan)
        res = execute(plan, rp, repo)
        b = build_evidence(plan, rp, res, repo, emb, apply_semantic_tiebreak=True)
        cand_ids = [p.id for p in res.candidates]
        gold = gold_sections(plan)
        vec = repo.vector_in_products(cand_ids, emb.embed(case.query),
                                      ("display", "gaming", "audio", "smart_features", "connectivity", "physical_design"))
        first_gold = next((i for i, c in enumerate(vec, 1) if c.section in gold), None) if gold else None
        passages = [x for p in b.products for x in p.passages if x.retrieval == "vector"]
        firsts = [p.passages[0] for p in b.products if p.passages and p.passages[0].retrieval == "vector"]
        structured = res.ranking.ranked_codes()
        final = b.semantic["tiebreak"]["final_order"]
        rel = list(case.relevant_product_ids)
        out.append({
            "id": case.id, "family": case.family, "candidates": len(cand_ids),
            "escaped_products": b.semantic["escaped_products"],
            "gold_sections": sorted(gold), "first_gold_section_chunk_rank": first_gold,
            "passage_hit@1": bool(first_gold and first_gold <= 1), "passage_hit@3": bool(first_gold and first_gold <= 3),
            "passage_hit@5": bool(first_gold and first_gold <= 5),
            "shortlist_passage_section_correct": (round(sum(x.section in gold for x in passages) / len(passages), 3)
                                                  if gold and passages else None),
            "top_passage_section_correct": (round(sum(x.section in gold for x in firsts) / len(firsts), 3)
                                            if gold and firsts else None),
            "structured_4b": _rank_metrics(structured, rel), "final_4c": _rank_metrics(final, rel),
            "moved_by_tiebreak": len(b.semantic["tiebreak"]["moved"]),
            "labelled_ranks": {c: {"structured": (structured.index(c) + 1 if c in structured else None),
                                   "final": (final.index(c) + 1 if c in final else None)} for c in rel},
            "confidence": b.retrieval_confidence, "approx_tokens": b.budget["approx_tokens"],
        })
    return out


def product_scoped_sections(repo, emb) -> list:
    rows = repo.search(Filters(), limit=200, cap=200).rows
    out = []
    for q, section, line in PRODUCT_SCOPED_QUERIES:
        v = emb.embed(q)
        ranks, line_ranks = [], []
        for p in rows:
            hits = repo.vector_in_products([p.id], v)
            secs = [c.section for c in hits]
            if section in secs:
                ranks.append(secs.index(section) + 1)
            if line:
                lr = next((i for i, c in enumerate(hits, 1) if line in c.content), None)
                if lr:
                    line_ranks.append(lr)
        out.append({"query": q, "gold_section": section, "products_with_section": len(ranks),
                    "section_rank1_rate": round(sum(r == 1 for r in ranks) / len(ranks), 3) if ranks else None,
                    "section_top3_rate": round(sum(r <= 3 for r in ranks) / len(ranks), 3) if ranks else None,
                    "mean_section_rank": round(mean(ranks), 2) if ranks else None,
                    "signal_line": line, "products_with_line": len(line_ranks),
                    "line_rank1_rate": round(sum(r == 1 for r in line_ranks) / len(line_ranks), 3) if line_ranks else None})
    return out


def probe_cases(repo, emb) -> dict:
    vocab = repo.vocabulary()
    rows = []
    for code, need, truth_pattern in PROBE_CASES:
        delta = QueryPlanDelta(intent=Intent.SPEC_QUESTION, model_refs=(ModelRef(code, RefKind.FULL),),
                               free_text_need=need)
        plan = resolve_plan(delta, repo, vocab)
        rp = route(plan)
        b = build_evidence(plan, rp, execute(plan, rp, repo), repo, emb)
        pid = plan.resolved_products[0].id
        with repo._conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM product_specs s WHERE s.product_id = %s AND "
                        "(s.spec_name || ': ' || COALESCE(s.spec_value, '')) ILIKE %s)", (pid, truth_pattern))
            truth = cur.fetchone()[0]
        kinds = {g.kind for g in b.gaps}
        p = b.products[0]
        status = ("mentioned" if any(r.startswith("spec_mentions") for r in p.selection_reasons) else
                  "verified_not_listed" if "attribute_not_listed_for_product" in kinds else
                  "absence_unverified" if "attribute_absence_unverified" in kinds else "none")
        all_terms = status == "mentioned" and "not all of" not in " ".join(p.selection_reasons)
        rows.append({"model_code": code, "need": need, "route": b.route, "truth_listed": truth, "probe": status,
                     "all_terms_matched": all_terms, "semantic_unavailable": "semantic_unavailable" in kinds,
                     "matched_spec_facts": [f.label + ": " + f.value for f in p.facts if f.origin == "product_specs"]})
    return {"cases": rows,
            "verified_present": sum(r["probe"] == "mentioned" and r["all_terms_matched"] and r["truth_listed"] for r in rows),
            "verified_absent": sum(r["probe"] == "verified_not_listed" and not r["truth_listed"] for r in rows),
            "unverified": sum(r["probe"] == "absence_unverified" for r in rows),
            "partial_mentions": sum(r["probe"] == "mentioned" and not r["all_terms_matched"] for r in rows),
            "false_absence": sum(r["probe"] == "verified_not_listed" and r["truth_listed"] for r in rows),
            "false_full_presence": sum(r["all_terms_matched"] and not r["truth_listed"] for r in rows)}


def corpus_analysis(repo, emb, plans) -> dict:
    out = {}
    cur = repo._conn.cursor()
    for key, terms in CORPUS_TERMS.items():
        case_id, query = LABELS[key]
        labels = [c for c in load_dataset() if c.id == case_id][0].relevant_product_ids
        term_rows = []
        for t in terms:
            cur.execute("SELECT count(DISTINCT c.product_id), count(DISTINCT c.product_id) FILTER (WHERE p.model_code = ANY(%s)) "
                        "FROM chunks c JOIN products p ON p.id = c.product_id WHERE c.content ILIKE %s",
                        (list(labels), f"%{t}%"))
            n, n_lab = cur.fetchone()
            term_rows.append({"term": t, "products": n, "labelled_products": n_lab, "labelled_total": len(labels)})
        plan, rp, res = run(plans[case_id], repo)
        cand = [p.id for p in res.candidates]
        code_of = {p.id: p.model_code for p in res.candidates}
        per_section = {}
        for secs in (None, ("display",), ("audio",)):
            hits = repo.vector_in_products(cand, emb.embed(query), secs or
                                           ("display", "gaming", "audio", "smart_features", "connectivity", "physical_design"))
            order = list(dict.fromkeys(code_of[c.product_id] for c in hits))
            sims = [c.similarity for c in hits]
            per_section["all_non_overview" if secs is None else secs[0]] = {
                "label_semantic_ranks": {l: (order.index(l) + 1 if l in order else None) for l in labels},
                "top5": order[:5], "similarity_top1": round(sims[0], 4) if sims else None,
                "similarity_10th": round(sims[9], 4) if len(sims) > 9 else None,
                "similarity_spread_top1_minus_median": round(sims[0] - sims[len(sims) // 2], 4) if sims else None}
        out[key] = {"labels": list(labels), "terms": term_rows, "semantic_within_candidates": per_section,
                    "candidates": len(cand)}
    cur.close()
    return out


def main(argv: Optional[list] = None) -> int:
    from .catalog_check import _connect

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    cases, plans = load_dataset(), load_gold_plans()
    emb = CachedQueryEmbeddings.from_file(CACHE)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            ro = cur.fetchone()[0]
        if ro != "on":
            raise SystemExit("refusing to run: session is not read-only")
        repo = CatalogRepository(conn)
        records, _ = evaluate(repo, cases, plans)
        attach_baseline(records)
        baseline_now = former_vector_primary_summary(records)
        baseline_4b = json.loads(PHASE_4B.read_text(encoding="utf-8"))["former_vector_primary"]
        cov = evidence_coverage(repo, cases, plans, emb)
        arm = experimental_arm(repo, cases, plans, emb)
        scoped = product_scoped_sections(repo, emb)
        probes = probe_cases(repo, emb)
        corpus = corpus_analysis(repo, emb, plans)
    finally:
        conn.rollback()
        conn.close()

    vp = [a for a in arm if a["family"] == "vector_primary"]
    hy = [a for a in arm if a["family"] == "hybrid"]
    def macro(rows, key):
        return {m: round(mean(float(r[key][m]) for r in rows), 4) for m in ("hit@1", "hit@3", "hit@5", "mrr")} if rows else None
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "db_transaction_read_only": ro,
           "openai_requests": 0, "embeddings_generated": 0,
           "cached_embedding_lookups": {"hits": emb.hits, "misses": emb.misses},
           "A_phase4b_baseline": {"recomputed": baseline_now, "committed": baseline_4b,
                                  "unchanged": baseline_now == baseline_4b},
           "B_evidence_coverage": cov,
           "C_candidate_scoped_experimental_arm": {"cases": arm,
                                                   "vector_primary_structured_4b": macro(vp, "structured_4b"),
                                                   "vector_primary_final_4c": macro(vp, "final_4c"),
                                                   "hybrid_structured_4b": macro(hy, "structured_4b"),
                                                   "hybrid_final_4c": macro(hy, "final_4c"),
                                                   "escape_violations": sum(a["escaped_products"] for a in arm)},
           "D_product_scoped_sections": scoped, "E_product_scoped_probes": probes, "F_corpus_analysis": corpus}
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("db_transaction_read_only", "cached_embedding_lookups", "A_phase4b_baseline")},
                     ensure_ascii=False, indent=1))
    print("\nB evidence coverage:")
    for r in cov:
        print(f"  {r['id']:36} {r['route']:17} conf={r['confidence']:8} prods={r['products']:2} "
              f"lab={len(r['labelled_in_evidence'])}/{r['labelled_total']} pass={r['passages']:2} {r['passage_paths']} "
              f"emb={r['embedding_lookups']} tok={r['approx_tokens']:5} gaps={r['gaps']}")
    print("\nC experimental arm (need text as free_text_need, candidate-scoped + fit-tie-only tie-break):")
    for a in arm:
        print(f"  {a['id']:36} cand={a['candidates']:2} esc={a['escaped_products']} gold={a['gold_sections']} "
              f"first_gold_chunk={a['first_gold_section_chunk_rank']} top_sec_ok={a['top_passage_section_correct']} "
              f"moved={a['moved_by_tiebreak']:2} ranks={a['labelled_ranks']}")
    print("  vector_primary 4B:", out["C_candidate_scoped_experimental_arm"]["vector_primary_structured_4b"],
          "\n  vector_primary 4C:", out["C_candidate_scoped_experimental_arm"]["vector_primary_final_4c"],
          "\n  hybrid 4B:", out["C_candidate_scoped_experimental_arm"]["hybrid_structured_4b"],
          "\n  hybrid 4C:", out["C_candidate_scoped_experimental_arm"]["hybrid_final_4c"])
    print("\nD product-scoped sections:")
    for s in scoped:
        print("  ", s)
    print("\nE probes:", {k: v for k, v in probes.items() if k != "cases"})
    for r in probes["cases"]:
        print("  ", {k: r[k] for k in ("model_code", "need", "truth_listed", "probe", "all_terms_matched")})
    print("\nF corpus:")
    for k, v in corpus.items():
        print(" ", k, [(t["term"], t["products"], f"{t['labelled_products']}/{t['labelled_total']}") for t in v["terms"]])
        for sec, d in v["semantic_within_candidates"].items():
            print("   ", sec, d)
    print(f"\nresults written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
