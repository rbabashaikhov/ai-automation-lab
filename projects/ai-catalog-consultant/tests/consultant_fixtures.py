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


DIMS = 1536
SECTION_AXIS = {"overview": 0, "display": 1, "gaming": 2, "audio": 3, "smart_features": 4,
                "connectivity": 5, "physical_design": 6}


def section_vector(section: str) -> list:
    """Synthetic query vector pointing at one section axis."""
    v = [0.0] * DIMS
    v[SECTION_AXIS[section]] = 1.0
    return v


def chunk_vector(section: str, product_index: int) -> list:
    """Synthetic chunk vector: its section axis plus a small product-specific component, so
    similarity to a section query is high for that section and distinct per product."""
    v = section_vector(section)
    v[20 + product_index] = 0.05 * (product_index % 9 + 1)
    return v


def seed_chunks(conn, ids: dict, null_embedding_code: str = "UE32H5000FUXRU") -> dict:
    """Real document/chunk text via the indexing builder; synthetic embeddings (no OpenAI).
    The ``null_embedding_code`` product's gaming-less layout gets one chunk with embedding NULL.
    Returns ``{(model_code, section): chunk_id}``."""
    from indexing.builder import build_document
    from indexing.chunker import build_chunks
    from indexing.models import Product, Spec

    out = {}
    with conn.cursor() as cur:
        for n, p in enumerate(load_raw()):
            prod = Product(ids[p["model_code"]], p["source"], p["external_id"], p["model_code"], p["name"], "Samsung",
                           p["category"], p["product_url"], p["year"], p["series"], p["screen_size_inches"],
                           p["resolution"], p["panel_technology"], p["refresh_rate_hz"], p["price"], p["sale_price"],
                           p["currency"], p["is_available"])
            specs = [Spec(s["spec_group"], s["spec_name"], s["spec_key"], s["spec_value"], s["sort_order"])
                     for s in p["specs"]]
            doc = build_document(prod, specs)
            cur.execute("INSERT INTO documents (product_id, document_type, title, content, content_hash) "
                        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                        (prod.id, doc.document_type, doc.title, doc.content, doc.content_hash))
            doc_id = cur.fetchone()[0]
            for ch in build_chunks(prod, specs):
                vec = None
                if not (p["model_code"] == null_embedding_code and ch.section == "physical_design"):
                    vec = "[" + ",".join(repr(x) for x in chunk_vector(ch.section, n)) + "]"
                cur.execute("INSERT INTO chunks (document_id, chunk_index, content, content_hash, metadata, embedding) "
                            "VALUES (%s,%s,%s,%s,%s,%s::vector) RETURNING id",
                            (doc_id, ch.chunk_index, ch.content, ch.content_hash,
                             json.dumps(ch.metadata, ensure_ascii=False), vec))
                out[(p["model_code"], ch.section)] = cur.fetchone()[0]
    conn.commit()
    return out


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
