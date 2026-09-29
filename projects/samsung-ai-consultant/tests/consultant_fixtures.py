"""Shared helpers for Phase 4B Consultant tests.

``consultant_catalog_subset.json`` is a curated read-only export of 31 real production products
with their registry-relevant specs (``python -m evaluation.consultant_inventory --fixture``).
``effective_price`` / ``product_kind`` in it were computed by the canonical SQL expressions, so
unit tests never re-implement those rules in Python.
"""

from __future__ import annotations

import json
from pathlib import Path

from consultant.schemas import ProductKind, ProductRow, SpecRow

FIXTURE = Path(__file__).parent / "fixtures" / "consultant_catalog_subset.json"


def load_raw() -> list:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["products"]


def product_rows() -> tuple:
    """``(rows, specs_by_id, by_code)`` with synthetic ids 1..N (fixture order)."""
    rows, specs, by_code = [], {}, {}
    for i, p in enumerate(load_raw(), start=1):
        row = ProductRow(i, p["source"], p["external_id"], p["model_code"], p["name"], p["category"],
                         p["series"], ProductKind(p["product_kind"]), p["year"], p["screen_size_inches"],
                         p["resolution"], p["panel_technology"], p["refresh_rate_hz"], p["price"],
                         p["sale_price"], p["effective_price"], p["currency"], p["is_available"],
                         p["product_url"])
        rows.append(row)
        by_code[row.model_code] = row
        specs[i] = [SpecRow(i, s["spec_group"], s["spec_name"], s["spec_key"], s["spec_value"]) for s in p["specs"]]
    return rows, specs, by_code


def seed(conn) -> dict:
    """Insert the fixture into a disposable database; returns ``{model_code: products.id}``."""
    ids = {}
    with conn.cursor() as cur:
        for p in load_raw():
            cur.execute(
                """INSERT INTO products (source, external_id, model_code, name, category, series, year,
                       screen_size_inches, resolution, panel_technology, refresh_rate_hz, price, sale_price,
                       currency, is_available, product_url)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (p["source"], p["external_id"], p["model_code"], p["name"], p["category"], p["series"],
                 p["year"], p["screen_size_inches"], p["resolution"], p["panel_technology"],
                 p["refresh_rate_hz"], p["price"], p["sale_price"], p["currency"], p["is_available"],
                 p["product_url"]))
            pid = cur.fetchone()[0]
            ids[p["model_code"]] = pid
            for s in p["specs"]:
                cur.execute("""INSERT INTO product_specs (product_id, spec_group, spec_name, spec_key,
                                   spec_value, sort_order) VALUES (%s,%s,%s,%s,%s,%s)""",
                            (pid, s["spec_group"], s["spec_name"], s["spec_key"], s["spec_value"], s["sort_order"]))
    conn.commit()
    return ids
