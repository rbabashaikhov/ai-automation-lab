"""Read-only validation of dataset expectations against the live catalog.

    python -m evaluation.catalog_check            # uses INDEXING_DATABASE_URL from .env

Every query is a SELECT inside a read-only session (``default_transaction_read_only``
+ ``readonly=True``); nothing is written and no credential is printed. The filter
compilation is pure (:func:`compile_filters`) and unit-tested without a database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from .dataset import EvalCase, load_dataset

EFFECTIVE_PRICE = "COALESCE(p.sale_price, p.price)"
_EQ = {"model_code": "p.model_code", "panel_technology": "p.panel_technology",
       "refresh_rate_hz": "p.refresh_rate_hz", "screen_size_inches": "p.screen_size_inches",
       "is_available": "p.is_available", "category": "p.category"}
_RANGE = {"min_screen_size_inches": ("p.screen_size_inches", ">="),
          "max_screen_size_inches": ("p.screen_size_inches", "<="),
          "max_effective_price": (EFFECTIVE_PRICE, "<=")}
_ORDER = ("screen_size_inches DESC", "effective_price ASC")


def compile_filters(filters: dict) -> dict:
    """Turn a case's ``filters`` into ``{where, params, order_by, order_expr}``.

    Unknown keys raise ``KeyError`` so a new filter type can never be silently ignored.
    """
    clauses, params, order = [], [], None
    for key, val in filters.items():
        if key in _EQ:
            clauses.append(f"{_EQ[key]} = %s")
            params.append(val)
        elif key in _RANGE:
            col, op = _RANGE[key]
            clauses.append(f"{col} {op} %s")
            params.append(val)
        elif key == "order_by":
            if val not in _ORDER:
                raise KeyError(f"unsupported order_by {val!r}")
            order = val
        elif key == "limit":
            continue  # handled as tie-aware top-N by the caller
        else:
            raise KeyError(f"unsupported filter key {key!r}")
    return {"where": " AND ".join(clauses) or "TRUE", "params": params, "order_by": order,
            "limit": filters.get("limit")}


def _connect():
    import psycopg2
    from dotenv import dotenv_values

    url = os.environ.get("INDEXING_DATABASE_URL") or dotenv_values(
        Path(__file__).resolve().parents[1] / ".env").get("INDEXING_DATABASE_URL")
    if not url:
        raise SystemExit("INDEXING_DATABASE_URL not set (env or .env)")
    conn = psycopg2.connect(url, connect_timeout=8, options="-c default_transaction_read_only=on")
    conn.set_session(readonly=True)
    return conn


def _codes(cur, cf: dict) -> list:
    sql = f"SELECT p.model_code FROM products p WHERE {cf['where']}"
    if cf["order_by"] and cf["limit"]:
        # tie-aware top-N: every product sharing the extreme value
        expr = ("p.screen_size_inches" if "screen" in cf["order_by"] else EFFECTIVE_PRICE)
        agg = "MAX" if cf["order_by"].endswith("DESC") else "MIN"
        sql += (f" AND {expr} = (SELECT {agg}({expr}) FROM products p WHERE {cf['where']})")
        cur.execute(sql, cf["params"] * 2)
    else:
        cur.execute(sql, cf["params"])
    return sorted(r[0] for r in cur.fetchall())


def check_case(cur, case: EvalCase) -> list:
    """Return a list of (ok, message) findings for one case."""
    out = []
    rel = list(case.relevant_product_ids)
    cur.execute("SELECT model_code, count(*), bool_and(is_available) FROM products "
                "WHERE model_code = ANY(%s) GROUP BY model_code", (rel,))
    found = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    for code in rel:
        n = found.get(code, (0, None))[0]
        if n != 1:
            out.append((False, f"model_code {code} matches {n} products (expected exactly 1)"))
    unavailable = [c for c, (_, av) in found.items() if av is False]
    if unavailable and case.filters.get("is_available") is not False:
        out.append((False, f"expected product(s) currently unavailable: {unavailable}"))
    if not case.filters:
        return out or [(True, "no structured filter to check; model codes exist and are unique")]
    got = _codes(cur, compile_filters(case.filters))
    if case.family in ("sql_sufficient", "aggregate_not_retrieval"):
        want = sorted(rel)
        out.append((got == want, f"SQL set == expected set: missing={sorted(set(want)-set(got))} "
                                 f"unexpected={sorted(set(got)-set(want))}"))
    else:
        excluded = sorted(set(rel) - set(got))
        out.append((not excluded, f"filter admits {len(got)} products; "
                                  f"expected excluded by filter={excluded}"))
    return out


def main(argv: Optional[list] = None) -> int:
    cases = load_dataset()
    bad = 0
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM products")
        print(f"products in catalog: {cur.fetchone()[0]}")
        for case in cases:
            for ok, msg in check_case(cur, case):
                bad += not ok
                print(f"[{'ok' if ok else 'MISMATCH':8}] {case.id}: {msg}")
    finally:
        conn.rollback()
        conn.close()
    print(f"\n{bad} mismatch(es)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
