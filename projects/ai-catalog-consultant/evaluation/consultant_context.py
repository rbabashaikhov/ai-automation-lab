"""Phase 3D.3 spike: build (not send) LLM evaluation contexts from CURRENT retrieval.

    python -m evaluation.consultant_context     # writes evaluation/results/consultant_spike_contexts.json

Read-only. No LLM/OpenAI call, no new embeddings (uses evaluation/results/query_embeddings.json),
no reranking, no query rewriting. Candidate selection is deterministic:

1. ``match_product_chunks`` (unchanged, unfiltered, all chunks) for the cached query embedding.
2. Candidates = the first ``K_PRODUCTS`` distinct products in chunk-rank order.
3. Each candidate's retrieved chunks = its best-ranked chunk plus any other of its chunks ranked
   within the first ``CHUNK_WINDOW`` chunks, at most ``MAX_CHUNKS_PER_PRODUCT``, in rank order.
4. Structured facts come from PostgreSQL via a fixed whitelist (same for every query); missing
   values are omitted, never filled in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .catalog_check import EFFECTIVE_PRICE, _connect
from .run_baseline import DEFAULT_CACHE, load_cache

QUERIES = (
    "телевизор для игровой приставки",
    "для светлой комнаты",
    "телевизор для фильмов",
    "хочу хороший звук без отдельного саундбара",
    "тонкий телевизор на стену",
)
K_PRODUCTS = 8
CHUNK_WINDOW = 40
MAX_CHUNKS_PER_PRODUCT = 3
OUT = Path(__file__).parent / "results" / "consultant_spike_contexts.json"

SPEC_WHITELIST = (
    "Технология экрана", "Поддержка форматов HDR", "Антибликовое покрытие", "Режимы просмотра",
    "Мощность звука, Вт", "Технологии улучшения звука", "Поддержка форматов звука",
    "Размер без подставки (ШxВxГ), см",
)
GAMING_GROUP = "Игровой режим"

SYSTEM_PROMPT = """You are a Samsung TV consultant. Answer in the language of the user's question.
Use ONLY the catalog context provided below. Rules:
- Recommend only products that appear in the context; never invent products, specs or prices.
- Explain briefly why each recommended product fits the request, citing concrete facts from the context (numbers such as Hz, W, inches, cm, price).
- State meaningful trade-offs (price, size, missing features, unavailability).
- 'Retrieved chunks' come from semantic search. Their similarity is not a quality score; do not say one TV is better because of it.
- If the context lacks the evidence needed to answer well (e.g. no candidate has the relevant feature, or a needed spec is missing), say so plainly instead of guessing.
- Give at most 3 recommendations."""


# ---- pure logic (unit-tested) ----------------------------------------------------------

def select_candidates(chunks: Sequence[dict], k_products: int = K_PRODUCTS,
                      window: int = CHUNK_WINDOW, max_chunks: int = MAX_CHUNKS_PER_PRODUCT) -> list:
    """``chunks`` are rank-ordered dicts with ``model_code``. Returns
    ``[{"model_code", "product_rank", "chunks": [chunk + {"chunk_rank"}]}]``."""
    order = []
    for c in chunks:
        if c["model_code"] not in order:
            order.append(c["model_code"])
        if len(order) == k_products:
            break
    out = []
    for pr, code in enumerate(order, start=1):
        mine = [dict(c, chunk_rank=i) for i, c in enumerate(chunks, start=1) if c["model_code"] == code]
        keep = [mine[0]] + [c for c in mine[1:] if c["chunk_rank"] <= window]
        out.append({"model_code": code, "product_rank": pr, "chunks": keep[:max_chunks]})
    return out


def parse_depth_cm(value: Optional[str]) -> Optional[float]:
    """Coverage diagnostics only: last number of a 'Ш x В x Г' string."""
    try:
        return float(str(value).replace(",", ".").split("x")[-1].strip())
    except (ValueError, AttributeError):
        return None


def render_context(candidates: Sequence[dict]) -> str:
    """Text block given to the LLM; structured facts and retrieved chunk text stay separate."""
    parts = []
    for c in candidates:
        f = c["facts"]
        lines = [f"### Candidate {c['product_rank']}: {f['model_code']} — {f['name']}",
                 "STRUCTURED FACTS (catalog database):"]
        lines += [f"- {k}: {v}" for k, v in f["fields"].items()]
        lines += [f"- {k}: {v}" for k, v in f["specs"].items()]
        lines.append("RETRIEVED CHUNKS (semantic search text; similarity is not a quality score):")
        for ch in c["chunks"]:
            lines.append(f"[section={ch['section']}, similarity={ch['similarity']:.3f}]\n{ch['content']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def build_user_message(query: str, candidates: Sequence[dict]) -> str:
    return f"User question: {query}\n\nCatalog context:\n\n{render_context(candidates)}"


# ---- DB access (read-only) --------------------------------------------------------------

def fetch_ranked_chunks(cur, embedding: Sequence[float]) -> list:
    lit = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    cur.execute("SELECT chunk_id, similarity, metadata->>'section', metadata->>'model_code', content "
                "FROM match_product_chunks(%s::vector, 5000)", (lit,))
    return [{"chunk_id": r[0], "similarity": float(r[1]), "section": r[2], "model_code": r[3],
             "content": r[4]} for r in cur.fetchall()]


def fetch_facts(cur, model_code: str) -> dict:
    cur.execute(f"""SELECT p.name, p.category, p.panel_technology, p.screen_size_inches, p.refresh_rate_hz,
                    p.price, p.sale_price, {EFFECTIVE_PRICE}, p.is_available, p.year
                    FROM products p WHERE p.model_code = %s""", (model_code,))
    n, cat, panel, size, hz, price, sale, eff, avail, year = cur.fetchone()
    raw = {"category": cat, "panel_technology": panel, "screen_size_inches": size,
           "refresh_rate_hz": hz, "price_rub": price, "sale_price_rub": sale,
           "effective_price_rub": eff, "is_available": avail, "year": year}
    fields = {k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in raw.items() if v is not None}
    cur.execute("""SELECT s.spec_name, s.spec_value FROM product_specs s JOIN products p ON p.id = s.product_id
                   WHERE p.model_code = %s AND (s.spec_name = ANY(%s) OR s.spec_group = %s)
                   ORDER BY s.sort_order, s.id""", (model_code, list(SPEC_WHITELIST), GAMING_GROUP))
    specs = {name: val for name, val in cur.fetchall() if val is not None}
    return {"model_code": model_code, "name": n, "fields": fields, "specs": specs}


# ---- coverage diagnostics (evaluator's reference sets; never fed to candidate selection) ------

REFERENCE_SQL = {
    "телевизор для игровой приставки":
        ("120 Hz panel AND FreeSync Premium* AND Game Bar",
         """SELECT p.model_code FROM products p WHERE p.refresh_rate_hz >= 120 AND EXISTS (
              SELECT 1 FROM product_specs s WHERE s.product_id=p.id AND s.spec_name='Технология FreeSync'
              AND s.spec_value ILIKE '%%Premium%%') AND EXISTS (
              SELECT 1 FROM product_specs s WHERE s.product_id=p.id AND s.spec_name ILIKE 'Игровая панель%%')"""),
    "для светлой комнаты":
        ("has 'Антибликовое покрытие' spec",
         """SELECT p.model_code FROM products p WHERE EXISTS (SELECT 1 FROM product_specs s
            WHERE s.product_id=p.id AND s.spec_name='Антибликовое покрытие')"""),
    "телевизор для фильмов":
        ("Filmmaker Mode / Режим режиссера in viewing modes AND Dolby Atmos",
         """SELECT p.model_code FROM products p WHERE EXISTS (SELECT 1 FROM product_specs s WHERE s.product_id=p.id
            AND s.spec_name='Режимы просмотра' AND (s.spec_value ILIKE '%%Filmmaker%%' OR s.spec_value ILIKE '%%режиссер%%'))
            AND EXISTS (SELECT 1 FROM product_specs s WHERE s.product_id=p.id
            AND s.spec_name='Поддержка форматов звука' AND s.spec_value ILIKE '%%Atmos%%')"""),
    "хочу хороший звук без отдельного саундбара":
        ("sound power >= 70 W",
         """SELECT p.model_code FROM products p JOIN product_specs s ON s.product_id=p.id
            WHERE s.spec_name='Мощность звука, Вт' AND s.spec_value ~ '^[0-9.]+$' AND s.spec_value::numeric >= 70"""),
    "тонкий телевизор на стену":
        ("depth without stand <= 3 cm (spec present for 59 products)", None),
}


def reference_set(cur, query: str) -> tuple:
    desc, sql = REFERENCE_SQL[query]
    if sql is None:
        cur.execute("SELECT p.model_code, s.spec_value FROM products p JOIN product_specs s ON s.product_id=p.id "
                    "WHERE s.spec_name='Размер без подставки (ШxВxГ), см'")
        codes = sorted(m for m, v in cur.fetchall() if (d := parse_depth_cm(v)) is not None and d <= 3)
    else:
        cur.execute(sql)
        codes = sorted(r[0] for r in cur.fetchall())
    return desc, codes


def main() -> int:
    cache = load_cache(DEFAULT_CACHE)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SHOW transaction_read_only")
        ro = cur.fetchone()[0]
        cur.execute("SELECT (SELECT count(*) FROM products), (SELECT count(*) FROM chunks), "
                    "(SELECT count(*) FROM chunks WHERE embedding IS NULL)")
        gate = dict(zip(("products", "chunks", "chunks_without_embedding"), cur.fetchone()), read_only=ro)
        cases = []
        for q in QUERIES:
            ranked = fetch_ranked_chunks(cur, cache[q])
            cands = select_candidates(ranked)
            for c in cands:
                c["facts"] = fetch_facts(cur, c["model_code"])
            desc, ref = reference_set(cur, q)
            got = [c["model_code"] for c in cands]
            first_rank = {}
            for i, ch in enumerate(ranked, start=1):
                first_rank.setdefault(ch["model_code"], i)
            prod_rank = {m: r for r, m in enumerate(first_rank, start=1)}
            cases.append({
                "query": q, "candidates": cands, "user_message": build_user_message(q, cands),
                "coverage": {
                    "candidate_products": got,
                    "sections_represented": sorted({ch["section"] for c in cands for ch in c["chunks"]}),
                    "reference_rule": desc, "reference_count": len(ref),
                    "reference_in_context": [m for m in ref if m in got],
                    "reference_missing_best_ranked": [(m, prod_rank[m]) for m in sorted(
                        (m for m in ref if m not in got), key=lambda m: prod_rank[m])][:8],
                }})
    finally:
        conn.rollback()
        conn.close()
    OUT.write_text(json.dumps({"gate": gate, "system_prompt": SYSTEM_PROMPT, "k_products": K_PRODUCTS,
                               "chunk_window": CHUNK_WINDOW, "max_chunks_per_product": MAX_CHUNKS_PER_PRODUCT,
                               "cases": cases}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("gate:", gate)
    for c in cases:
        cv = c["coverage"]
        print(f"\n## {c['query']}\n candidates: {cv['candidate_products']}\n sections: {cv['sections_represented']}")
        print(f" reference [{cv['reference_rule']}]: {cv['reference_count']} products; in context: {cv['reference_in_context']}")
        print(f" reference missing (best ranks): {cv['reference_missing_best_ranked']}")
        print(f" message chars: {len(c['user_message'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
