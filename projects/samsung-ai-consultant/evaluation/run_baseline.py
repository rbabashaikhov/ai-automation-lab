"""Phase 3D.2 real retrieval baseline: an evaluation composition, not a retrieval layer.

    python -m evaluation.run_baseline [--embeddings-cache PATH] [--skip-vector] [--out PATH]

Read-only end to end (SELECT-only read-only session). Mechanisms per family:

* sql_sufficient / aggregate_not_retrieval: deterministic SQL only (no vectors, no embeddings).
* vector_primary: the case query embedded as-is, ``match_product_chunks`` with no filters.
* hybrid (and difficult cases that carry structured filters): SQL builds the candidate
  product set from the case filters (``catalog_check.compile_filters``; effective price);
  ``match_product_chunks`` is called unfiltered over all chunks; chunks of non-candidate
  products are dropped (:func:`compose_hybrid`) and products are ranked by their best chunk.
* difficult_ambiguous: same mechanism as above, results informational.

Query embeddings come from a JSON cache ``{query_text: [1536 floats]}``. Missing queries
are fetched once, in a single OpenAI request, when OPENAI_API_KEY is set in the environment
(never printed or stored in results); otherwise vector cases are reported as not run.
Stored chunk embeddings are never written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from . import metrics
from .catalog_check import _codes, _connect, compile_filters
from .dataset import EvalCase, load_dataset
from .scoring import CaseRun, ChunkHit, render_summary, score_case, summarize

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = 1536
ALL_CHUNKS = 5000  # far above the ~525 chunks in the catalog; exact scan returns them all
DEFAULT_CACHE = Path(__file__).parent / "results" / "query_embeddings.json"
DEFAULT_OUT = Path(__file__).parent / "results" / "baseline_3d2.json"
TOP_N = 5


# ---- pure logic (unit-tested) -------------------------------------------------------

def compose_hybrid(chunks: Sequence[ChunkHit], candidates: Sequence[str]) -> list:
    """Keep chunks of SQL-candidate products, preserving similarity order."""
    allowed = set(candidates)
    return [c for c in chunks if c.model_code in allowed]


def sql_only_discriminates(candidates: Sequence[str], relevant: Sequence[str]) -> bool:
    """False when every SQL candidate is an acceptable answer, i.e. the structured filter
    alone already satisfies Hit@K for any ordering and semantic ranking cannot be observed."""
    return not set(candidates) <= set(relevant)


def expected_ranks(ranked: Sequence[str], relevant: Sequence[str]) -> dict:
    d = metrics.dedupe_ranked(ranked)
    return {p: (d.index(p) + 1 if p in d else None) for p in relevant}


def load_cache(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    for q, v in data.items():
        if len(v) != EMBEDDING_DIMS:
            raise ValueError(f"cached embedding for {q!r} has {len(v)} dims, expected {EMBEDDING_DIMS}")
    return data


def queries_needing_embedding(cases: Sequence[EvalCase]) -> list:
    """Distinct query texts (case order) for every case that uses vector retrieval."""
    out = []
    for c in cases:
        if c.expected_mechanism != "aggregate_not_retrieval" and c.family not in ("sql_sufficient",):
            if c.query not in out:
                out.append(c.query)
    return out


def extract_query_embeddings(execution: dict, expected_queries: Sequence[str],
                             emit_node: str = "Emit evaluation queries",
                             embed_node: str = "OpenAI: Embed query") -> dict:
    """Pull ``{query_text: embedding}`` out of an n8n execution (``includeData=true``) and verify it.

    Texts come from ``emit_node`` and vectors from ``embed_node``, paired by ``pairedItem``
    (the workflow's own validation node only inspects the first item, so every vector is
    validated here). Requires exactly one item per expected query (same texts, no extras or
    duplicates), 1536 dimensions each, and the ``text-embedding-3-small`` model. Only these
    fields are kept.
    """
    if execution.get("status") not in (None, "success"):
        raise ValueError(f"execution status is {execution.get('status')!r}, expected success")
    run_data = execution["data"]["resultData"]["runData"]
    texts = [i["json"]["query_text"] for r in run_data[emit_node] for i in r["data"]["main"][0]]
    if sorted(texts) != sorted(expected_queries):
        raise ValueError(f"query texts differ from the dataset: got {len(texts)} items")
    outputs = [i for r in run_data[embed_node] for i in r["data"]["main"][0]]
    if len(outputs) != len(texts):
        raise ValueError(f"{len(outputs)} embedding outputs for {len(texts)} queries")
    out = {}
    for pos, item in enumerate(outputs):
        idx = (item.get("pairedItem") or {}).get("item", pos)
        entries = item["json"].get("data") or []
        if len(entries) != 1 or not str(item["json"].get("model", "")).startswith(EMBEDDING_MODEL):
            raise ValueError(f"unexpected embedding response for query {texts[idx]!r}")
        vec = entries[0]["embedding"]
        if len(vec) != EMBEDDING_DIMS:
            raise ValueError(f"bad dimensions ({len(vec)}) for {texts[idx]!r}")
        out[texts[idx]] = vec
    if len(out) != len(expected_queries):
        raise ValueError("duplicate or unpaired query texts in execution")
    return out


# ---- I/O ----------------------------------------------------------------------------

def fetch_embeddings(texts: Sequence[str]) -> list:
    """One OpenAI request for all texts. Key from env only; never logged."""
    import requests

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    r = requests.post("https://api.openai.com/v1/embeddings", timeout=60,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": EMBEDDING_MODEL, "input": list(texts)})
    r.raise_for_status()
    rows = sorted(r.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in rows]


def get_embeddings(queries: Sequence[str], cache_path: Path) -> tuple:
    """Return (embeddings by query, number of OpenAI requests, number of texts embedded)."""
    cache = load_cache(cache_path)
    missing = [q for q in queries if q not in cache]
    calls = 0
    if missing and os.environ.get("OPENAI_API_KEY"):
        for q, v in zip(missing, fetch_embeddings(missing)):
            cache[q] = v
        calls = 1
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
    return cache, calls, len(missing) if calls else 0


def vector_chunks(cur, embedding: Sequence[float], identity: dict) -> list:
    lit = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    cur.execute("SELECT chunk_id, similarity, metadata->>'section', metadata->>'model_code' "
                "FROM match_product_chunks(%s::vector, %s)", (lit, ALL_CHUNKS))
    return [ChunkHit(model_code=r[3], section=r[2], similarity=float(r[1])) for r in cur.fetchall()]


def _chunk_rows(chunks: Sequence[ChunkHit], identity: dict, n: int = TOP_N) -> list:
    return [{"rank": i, "model_code": c.model_code, "identity": identity.get(c.model_code),
             "section": c.section, "similarity": round(c.similarity, 4)}
            for i, c in enumerate(chunks[:n], start=1)]


# ---- runner -------------------------------------------------------------------------

def run_case(cur, case: EvalCase, emb: Optional[Sequence[float]], identity: dict) -> dict:
    rel = list(case.relevant_product_ids)
    rec = {"id": case.id, "family": case.family, "query": case.query, "filters": case.filters,
           "expected": rel}
    fam = case.family
    if fam in ("sql_sufficient", "aggregate_not_retrieval"):
        got = _codes(cur, compile_filters(case.filters))
        run = CaseRun(case.id, "sql", got)
        rec.update(mechanism="sql", actual=got)
        res = score_case(case, run)
    else:
        cand = None
        if case.filters:
            cand = _codes(cur, compile_filters(case.filters))
            key = "supplementary_sql_candidates" if fam == "vector_primary" else "sql_candidates"
            rec[key] = {"count": len(cand), "products": cand}
            if fam != "vector_primary":
                rec["sql_only_discriminates"] = sql_only_discriminates(cand, rel)
        if emb is None:
            rec.update(mechanism="not_run", reason="no query embedding available")
            res = score_case(case, None)
        else:
            chunks = vector_chunks(cur, emb, identity)
            rec["unfiltered_top5_chunks"] = _chunk_rows(chunks, identity)
            unfiltered = chunks
            if cand is not None and fam != "vector_primary":
                chunks = compose_hybrid(chunks, cand)  # vector_primary is always scored unfiltered
            mech = "hybrid" if cand is not None and fam != "vector_primary" else "vector"
            run = CaseRun(case.id, mech, chunks=chunks, filter_admitted=cand)
            ranked = run.ranked_products()
            rec.update(mechanism=mech, top5_chunks=_chunk_rows(chunks, identity),
                       top5_products=ranked[:TOP_N], expected_product_ranks=expected_ranks(ranked, rel))
            res = score_case(case, run)
            if fam == "vector_primary" and cand is not None:
                # supplementary, unscored: the case's own filter applied to the same embedding
                fc = compose_hybrid(unfiltered, cand)
                rec["supplementary_with_case_filter_top5_products"] = metrics.dedupe_ranked(
                    [c.model_code for c in fc])[:TOP_N]
    rec.update(status=res.status, metrics=res.metrics, checks=[list(c) for c in res.checks])
    rec["_result"] = res
    return rec


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embeddings-cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--skip-vector", action="store_true")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    cases = load_dataset()
    cache, calls, embedded = ({}, 0, 0) if args.skip_vector else get_embeddings(
        queries_needing_embedding(cases), args.embeddings_cache)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SHOW transaction_read_only")
        read_only = cur.fetchone()[0]
        cur.execute("SELECT model_code, source, external_id FROM products")
        identity = {m: {"source": s, "external_id": e} for m, s, e in cur.fetchall()}
        cur.execute("SELECT count(*), count(*) FILTER (WHERE embedding IS NULL) FROM chunks")
        n_chunks, n_null = cur.fetchone()
        records = [run_case(cur, c, cache.get(c.query), identity) for c in cases]
    finally:
        conn.rollback()
        conn.close()

    results = [r.pop("_result") for r in records]
    summary = summarize(results)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "db_transaction_read_only": read_only, "products": len(identity),
           "chunks": n_chunks, "chunks_without_embedding": n_null,
           "embedding_model": EMBEDDING_MODEL, "openai_requests": calls,
           "query_texts_embedded": embedded, "cases": records, "summary": summary}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"read_only={read_only} products={len(identity)} chunks={n_chunks} "
          f"chunks_without_embedding={n_null} openai_requests={calls} texts_embedded={embedded}")
    print("\n".join(r.render() for r in results))
    print(render_summary(summary))
    print(f"\nresults written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
