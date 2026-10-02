"""Read-only catalog fingerprint, run inside the Consultant container with its own least-privilege role:

    docker exec -i samsung-consultant python - < catalog_fingerprint.py

Prints one JSON object: row counts, the newest ``updated_at`` values, the embedding model and dimension, the
server versions, and md5 digests of the products, spec rows and chunks. Two runs that print the same object saw
the same catalog. It is how "no catalog mutation" is checked around a deployment or cleanup.

Nothing is written: the role is ``samsung_consultant`` (SELECT on products, product_specs and chunks; sessions are
read-only by default) and every statement runs inside a read-only transaction that is rolled back. No credential
is printed: the DSN is read from the container's environment and never echoed.

``products_md5_code_price_sale_availability``: one ``model_code:price:sale_price:is_available`` line per product,
ordered by model code, joined with newlines. It reproduces the products digest recorded in the Phase 4F.3 run
manifest (``34f4d64c…``). The other three digests are defined here and are comparable only between runs of this
script.
"""
import json
import os

from consultant.catalog_repository import open_readonly_connection

QUERIES = {
    "products": "SELECT count(*) FROM products",
    "products_available": "SELECT count(*) FROM products WHERE is_available",
    "sources": "SELECT string_agg(DISTINCT source, ',' ORDER BY source) FROM products",
    "products_max_updated_at": "SELECT to_char(max(updated_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM products",
    "product_specs": "SELECT count(*) FROM product_specs",
    "documents": "SELECT count(DISTINCT document_id) FROM chunks",
    "chunks": "SELECT count(*) FROM chunks",
    "chunks_embedded": "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL",
    "embedding_models": "SELECT string_agg(DISTINCT embedding_model, ',' ORDER BY embedding_model) FROM chunks",
    "embedding_dimensions": "SELECT string_agg(DISTINCT vector_dims(embedding)::text, ',') FROM chunks WHERE embedding IS NOT NULL",
    "postgresql": "SHOW server_version",
    "pgvector": "SELECT extversion FROM pg_extension WHERE extname = 'vector'",
    "transaction_read_only": "SHOW transaction_read_only",
    "role": "SELECT current_user",
    "products_md5_code_price_sale_availability":
        "SELECT md5(string_agg(model_code || ':' || coalesce(price::text, '') || ':' || coalesce(sale_price::text, '') || ':' || is_available, "
        "E'\\n' ORDER BY model_code)) FROM products",
    "products_md5": "SELECT md5(string_agg(md5(p::text), '' ORDER BY p.id)) FROM products p",
    "product_specs_md5": "SELECT md5(string_agg(md5(s::text), '' ORDER BY s.id)) FROM product_specs s",
    "chunks_md5": "SELECT md5(string_agg(md5(c.id || ':' || c.content_hash || ':' || md5(c.embedding::text)), '' ORDER BY c.id)) "
                  "FROM chunks c",
}


def main() -> None:
    conn = open_readonly_connection(os.environ["CONSULTANT_DATABASE_URL"])
    out = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '30s'")
            for name, sql in QUERIES.items():
                cur.execute(sql)
                out[name] = cur.fetchone()[0]
    finally:
        conn.rollback()
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
