"""Phase 4B read-only catalog inventory for the Consultant feature registry.

    python -m evaluation.consultant_inventory            # writes results/consultant_inventory_4b.json
    python -m evaluation.consultant_inventory --fixture  # also refreshes tests/fixtures/consultant_catalog_subset.json

SELECT-only, inside the same read-only session as ``catalog_check`` (``default_transaction_read_only``
+ ``readonly=True``). Nothing is written to the database and no credential is printed. The output is
aggregate evidence (distinct values and counts), not a catalog export; the optional fixture is a small
curated subset (~30 products, registry-relevant specs only) used by the disposable-DB tests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

from consultant.catalog_repository import EFFECTIVE_PRICE_SQL, PRODUCT_KIND_SQL
from consultant.features import REGISTRY_SPEC_NAMES

from .catalog_check import _connect

OUT = Path(__file__).parent / "results" / "consultant_inventory_4b.json"
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "consultant_catalog_subset.json"

# Products chosen to cover every registry feature state, price rule, tie and data-quality case
# observed in the inventory (see docs/PHASE_4B_STRUCTURED_CORE.md).
FIXTURE_CODES = (
    "QE65S95HAUXPY", "QE65S90HAEXPY", "QE65S85HAEXPY", "QE55S95HAUXPY", "QE77S95HAEXPY",
    "QE83S95HAEXPY", "QE83S90HAEXPY", "QE83S85HAEXPY", "QE48S85HAEXPY", "QE42S90HAEXPY",
    "QE55S85HAEXPY", "QE55QN80HAUXPY", "QE65QN80HAUXPY", "QE75QN80HAUXPY", "QE75QN70HAUXPY",
    "QE75LS03HWUXPY", "QE55LS03HAUXPY", "QE55QN1EHAUXPY", "QE32LS03CBUXRU", "UE32H5000FUXRU",
    "UE32F6000FUXRU", "QE32Q5FAAUXPY", "UE43F6000FUXRU", "UE75M70HAUXPY", "UE75U8000HUXPY",
    "MRE75R85HAUXPY", "MRE55R85HAUXPY", "MRE100R85HUXPY", "MRE115MR95FXRU", "MNA114MS1CCXRU",
    "UE27LSM7FAXXPY",
)
DIM_SPEC = "Размер без подставки (ШxВxГ), см"


def _rows(cur, sql: str, params: tuple = ()) -> list:
    cur.execute(sql, params)
    return [list(r) for r in cur.fetchall()]


def collect(cur) -> dict:
    out: dict = {}
    out["products"] = _rows(cur, "SELECT count(*), count(*) FILTER (WHERE is_available) FROM products")[0]
    out["series"] = _rows(cur, """SELECT series, count(*), array_agg(model_code ORDER BY screen_size_inches)
                                   FROM products GROUP BY series ORDER BY series""")
    out["product_kind"] = _rows(cur, f"SELECT {PRODUCT_KIND_SQL} AS k, count(*), array_agg(p.model_code) "
                                     "FROM products p GROUP BY k ORDER BY k")
    out["category_panel_resolution"] = _rows(cur, """SELECT category, panel_technology, resolution, count(*)
                                                     FROM products GROUP BY 1,2,3 ORDER BY 1,2,3""")
    out["spec_names"] = _rows(cur, """SELECT spec_group, spec_name, count(DISTINCT product_id)
                                       FROM product_specs GROUP BY 1,2 ORDER BY 1,2""")
    out["registry_spec_values"] = {
        name: _rows(cur, """SELECT spec_value, count(*) FROM product_specs WHERE spec_name = %s
                            GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 15""", (name,))
        for name in REGISTRY_SPEC_NAMES if name != DIM_SPEC
    }
    out["text_search_products"] = {
        pat: _rows(cur, """SELECT spec_name, count(DISTINCT product_id) FROM product_specs
                           WHERE spec_value ILIKE %s GROUP BY 1 ORDER BY 2 DESC""", (pat,))
        for pat in ("%Variable Refresh%", "%VRR%", "%ALLM%", "%HDMI 2%", "%Filmmaker%", "%Atmos%")
    }
    ok = re.compile(r"^\d+(\.\d+)? x \d+(\.\d+)? x \d+(\.\d+)?$")
    dims = _rows(cur, """SELECT p.model_code, p.screen_size_inches, s.spec_value FROM product_specs s
                         JOIN products p ON p.id = s.product_id WHERE s.spec_name = %s ORDER BY 1""", (DIM_SPEC,))
    out["dimensions"] = {"rows": len(dims),
                         "non_canonical": [[m, v] for m, _, v in dims if not ok.match(v)]}
    out["effective_price"] = _rows(cur, f"""SELECT count(*) FILTER (WHERE sale_price IS NOT NULL),
                                            count(*) FILTER (WHERE sale_price >= price),
                                            min({EFFECTIVE_PRICE_SQL}), max({EFFECTIVE_PRICE_SQL})
                                            FROM products p""")[0]
    return out


def fixture(cur) -> dict:
    cur.execute(f"""SELECT p.id, p.source, p.external_id, p.model_code, p.name, p.category, p.series,
                    p.year, p.screen_size_inches, p.resolution, p.panel_technology, p.refresh_rate_hz,
                    p.price, p.sale_price, {EFFECTIVE_PRICE_SQL}, p.currency, p.is_available, p.product_url,
                    {PRODUCT_KIND_SQL}
                    FROM products p WHERE p.model_code = ANY(%s) ORDER BY p.model_code""", (list(FIXTURE_CODES),))
    cols = ("id", "source", "external_id", "model_code", "name", "category", "series", "year",
            "screen_size_inches", "resolution", "panel_technology", "refresh_rate_hz", "price",
            "sale_price", "effective_price", "currency", "is_available", "product_url", "product_kind")
    products = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.execute("""SELECT p.model_code, s.spec_group, s.spec_name, s.spec_key, s.spec_value, s.sort_order
                   FROM product_specs s JOIN products p ON p.id = s.product_id
                   WHERE p.model_code = ANY(%s) AND s.spec_name = ANY(%s)
                   ORDER BY p.model_code, s.sort_order, s.id""", (list(FIXTURE_CODES), list(REGISTRY_SPEC_NAMES)))
    specs: dict = {}
    for code, group, name, key, value, order in cur.fetchall():
        specs.setdefault(code, []).append({"spec_group": group, "spec_name": name, "spec_key": key,
                                           "spec_value": value, "sort_order": order})
    for p in products:
        p.pop("id")
        for k in ("screen_size_inches", "price", "sale_price", "effective_price"):
            p[k] = None if p[k] is None else float(p[k])
        p["specs"] = specs.get(p["model_code"], [])
    return {"_meta": {"description": "Curated subset of the production samsung_rag catalog (read-only export, "
                                     "registry-relevant specs only) for Phase 4B disposable-DB tests. "
                                     "effective_price and product_kind were computed by the canonical SQL expressions.",
                      "generated_by": "python -m evaluation.consultant_inventory --fixture",
                      "registry_spec_names": list(REGISTRY_SPEC_NAMES)},
            "products": products}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", action="store_true")
    args = ap.parse_args(argv)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SHOW transaction_read_only")
        ro = cur.fetchone()[0]
        if ro != "on":
            raise SystemExit("refusing to run: session is not read-only")
        data = {"db_transaction_read_only": ro, **collect(cur)}
        fx = fixture(cur) if args.fixture else None
    finally:
        conn.rollback()
        conn.close()
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"read_only={ro} products={data['products']} spec_names={len(data['spec_names'])} -> {OUT}")
    if fx is not None:
        FIXTURE.write_text(json.dumps(fx, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"fixture: {len(fx['products'])} products -> {FIXTURE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
