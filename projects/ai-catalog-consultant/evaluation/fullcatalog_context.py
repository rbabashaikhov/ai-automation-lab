"""Phase 3D.4 control experiment: full-catalog context (evaluation-only upper bound).

    python -m evaluation.fullcatalog_context    # writes evaluation/results/fullcatalog_spike_contexts.json

Read-only; no LLM call. Same five queries and system prompt as Phase 3D.3. Every AVAILABLE product
appears exactly once as a compact structured fact sheet, ordered by model_code (no ranking, no
scoring, no intent parsing). Absent whitelisted values are rendered as ``not listed``; catalog
values are preserved raw (malformed dimension strings are reported, never corrected). Semantic
chunks (current retrieval, unchanged) are attached only as separate supporting evidence for the
same top-8 products as in 3D.3; they never decide which products are in the fact sheet.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

from .catalog_check import EFFECTIVE_PRICE, _connect
from .consultant_context import (
    QUERIES, SYSTEM_PROMPT, fetch_ranked_chunks, reference_set, select_candidates,
)
from .run_baseline import DEFAULT_CACHE, load_cache

OUT = Path(__file__).parent / "results" / "fullcatalog_spike_contexts.json"
NOT_LISTED = "not listed"

# (group, label, catalog spec_name) -- one fixed whitelist for every query
SPEC_FIELDS = (
    ("gaming", "game_mode", "Игровой режим"),
    ("gaming", "ALLM", "Автовключение игрового режима (ALLM)"),
    ("gaming", "Game_Motion_Plus", "Функция Game Motion Plus"),
    ("gaming", "FreeSync", "Технология FreeSync"),
    ("gaming", "Game_Bar", "Игровая панель (Game Bar)"),
    ("gaming", "Super_Ultra_Wide_Game_View", "Ультраширокий обзор в игровом режиме (Super Ultra Wide Game View)"),
    ("display", "HDR_formats", "Поддержка форматов HDR"),
    ("display", "viewing_modes", "Режимы просмотра"),
    ("display", "anti_glare", "Антибликовое покрытие"),
    ("audio", "sound_power_W", "Мощность звука, Вт"),
    ("audio", "sound_formats", "Поддержка форматов звука"),
    ("audio", "sound_technologies", "Технологии улучшения звука"),
    ("dimensions", "size_without_stand_WxHxD_cm", "Размер без подставки (ШxВxГ), см"),
)
LEGEND = ("Legend: each product below is one catalog entry (all currently available products are listed, "
          "sorted by model code, not ranked). 'not listed' means the catalog has no value for that field; "
          "it does not mean the feature is present or absent. Values are raw catalog data.")

_DIM_OK = re.compile(r"^\d+(\.\d+)? x \d+(\.\d+)? x \d+(\.\d+)?$")


# ---- pure logic (unit-tested) ----------------------------------------------------------------

def _v(x) -> str:
    return NOT_LISTED if x is None or str(x).strip() == "" else str(x)


def render_product(p: dict) -> str:
    """One compact block. ``p``: model_code, name, typed columns, ``specs`` {spec_name: value}."""
    price = _v(p.get("effective_price"))
    if p.get("sale_price") is not None:
        price += f" (list {_v(p.get('price'))})"
    lines = [f"### {p['model_code']} — {_v(p.get('name'))}",
             f"general: category={_v(p.get('category'))}; panel={_v(p.get('panel_technology'))}; "
             f"size_inches={_v(p.get('screen_size_inches'))}; refresh_hz={_v(p.get('refresh_rate_hz'))}; "
             f"effective_price_rub={price}; available={'yes' if p.get('is_available') else 'no'}"]
    for group in ("gaming", "display", "audio", "dimensions"):
        items = [f"{label}={_v(p['specs'].get(name))}" for g, label, name in SPEC_FIELDS if g == group]
        lines.append(f"{group}: " + "; ".join(items))
    return "\n".join(lines)


def render_fact_sheet(products: Sequence[dict]) -> str:
    return "\n\n".join(render_product(p) for p in products)


def render_supporting_chunks(candidates: Sequence[dict]) -> str:
    out = []
    for c in candidates:
        for ch in c["chunks"]:
            out.append(f"[{c['model_code']} | section={ch['section']} | similarity={ch['similarity']:.3f}]\n{ch['content']}")
    return "\n\n".join(out)


def build_message(query: str, products: Sequence[dict], candidates: Sequence[dict]) -> str:
    return (f"User question: {query}\n\n{LEGEND}\n\nCatalog fact sheet ({len(products)} available products):\n\n"
            f"{render_fact_sheet(products)}\n\n"
            "Supporting retrieved chunks (semantic search text for the closest matches; similarity is not a quality "
            "score; the catalog fact sheet above is the complete product list):\n\n"
            f"{render_supporting_chunks(candidates)}")


def represented_once(message: str, model_codes: Sequence[str]) -> dict:
    """Each expected code must head exactly one fact-sheet block; no others may appear."""
    heads = re.findall(r"^### (\S+) — ", message, flags=re.M)
    missing = [m for m in model_codes if heads.count(m) == 0]
    dup = sorted({m for m in heads if heads.count(m) > 1})
    extra = sorted(set(heads) - set(model_codes))
    return {"ok": not (missing or dup or extra) and len(heads) == len(model_codes),
            "blocks": len(heads), "missing": missing, "duplicated": dup, "extra": extra}


def malformed_dimensions(products: Sequence[dict]) -> list:
    """Raw dimension strings that do not match 'N x N x N' (reported, never corrected)."""
    key = "Размер без подставки (ШxВxГ), см"
    return [(p["model_code"], p["specs"][key]) for p in products
            if p["specs"].get(key) is not None and not _DIM_OK.match(p["specs"][key])]


# ---- DB access (read-only) ---------------------------------------------------------------------

def fetch_available_products(cur) -> list:
    cur.execute(f"""SELECT p.id, p.model_code, p.name, p.category, p.panel_technology, p.screen_size_inches,
                    p.refresh_rate_hz, p.price, p.sale_price, {EFFECTIVE_PRICE}, p.is_available
                    FROM products p WHERE p.is_available ORDER BY p.model_code""")
    cols = ("id", "model_code", "name", "category", "panel_technology", "screen_size_inches",
            "refresh_rate_hz", "price", "sale_price", "effective_price", "is_available")
    prods = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        for k in ("screen_size_inches", "price", "sale_price", "effective_price"):
            if d[k] is not None:
                d[k] = int(d[k]) if d[k] == int(d[k]) else float(d[k])
        d["specs"] = {}
        prods.append(d)
    by_id = {p["id"]: p for p in prods}
    cur.execute("SELECT product_id, spec_name, spec_value FROM product_specs WHERE product_id = ANY(%s) "
                "AND spec_name = ANY(%s)", (list(by_id), [n for _, _, n in SPEC_FIELDS]))
    for pid, name, val in cur.fetchall():
        by_id[pid]["specs"][name] = val
    return prods


def count_tokens(text: str) -> Optional[int]:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:  # tokenizer is optional
        return None


def main() -> int:
    cache = load_cache(DEFAULT_CACHE)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SHOW transaction_read_only")
        ro = cur.fetchone()[0]
        cur.execute("SELECT (SELECT count(*) FROM products), (SELECT count(*) FROM products WHERE is_available), "
                    "(SELECT count(*) FROM chunks), (SELECT count(*) FROM chunks WHERE embedding IS NULL)")
        gate = dict(zip(("products", "available_products", "chunks", "chunks_without_embedding"), cur.fetchone()),
                    read_only=ro)
        products = fetch_available_products(cur)
        codes = [p["model_code"] for p in products]
        cases = []
        for q in QUERIES:
            cands = select_candidates(fetch_ranked_chunks(cur, cache[q]))
            msg = build_message(q, products, cands)
            desc, ref = reference_set(cur, q)
            ref_sets = {"rule": desc, "reference_count": len(ref),
                        "unavailable_excluded": [m for m in ref if m not in codes]}
            cases.append({"query": q, "user_message": msg, "supporting_products": [c["model_code"] for c in cands],
                          "represented": represented_once(msg, codes), "reference": ref_sets,
                          "reference_present": [m for m in ref if m in codes and represented_once(msg, [m])["blocks"] >= 1],
                          "chars": len(msg), "tokens_user": count_tokens(msg)})
        sp_tokens = count_tokens(SYSTEM_PROMPT)
        cur.execute("SELECT model_code FROM products WHERE model_code LIKE 'QE%%S95H%%' ORDER BY 1")
        s95 = [r[0] for r in cur.fetchall()]
    finally:
        conn.rollback()
        conn.close()
    OUT.write_text(json.dumps({"gate": gate, "system_prompt": SYSTEM_PROMPT, "available_products": codes,
                               "malformed_dimensions": malformed_dimensions(products), "s95h_models": s95,
                               "cases": cases}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("gate:", gate, "| system prompt tokens:", sp_tokens)
    print("fact sheet only chars/tokens:", len(render_fact_sheet(products)), count_tokens(render_fact_sheet(products)))
    for c in cases:
        print(f"\n## {c['query']}\n chars={c['chars']} user_tokens={c['tokens_user']} represented={c['represented']}")
        print(f" reference [{c['reference']['rule']}]: {c['reference']['reference_count']} total, "
              f"{len(c['reference_present'])} present in sheet, unavailable(excluded)={c['reference']['unavailable_excluded']}")
    print("\nmalformed dimension values:", malformed_dimensions(products))
    print("S95H models:", s95, "| in available sheet:", [m for m in s95 if m in codes])
    return 0


if __name__ == "__main__":
    sys.exit(main())
