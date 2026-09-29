"""Phase 4D.2A write-denial check, run *inside* the consultant container with its own role:

    docker exec -i samsung-consultant python - < deploy/consultant/verify_readonly.py

Every write attempt is harmless by construction (``WHERE false`` / objects that would be rolled
back) and every transaction is rolled back. Two layers are checked separately: the role's
read-only session default, and -- after a client explicitly asks for READ WRITE, which any
client can do -- the grants themselves. Prints outcomes (SQLSTATE names) only.
"""

import json
import os

import psycopg2
from psycopg2 import errorcodes

conn = psycopg2.connect(os.environ["CONSULTANT_DATABASE_URL"], connect_timeout=8)
cur = conn.cursor()
out = {}
cur.execute("SELECT current_user, current_setting('transaction_read_only'), "
            "(SELECT count(*) FROM products), (SELECT count(*) FROM product_specs), (SELECT count(*) FROM chunks)")
user, ro, n_products, n_specs, n_chunks = cur.fetchone()
out["role"] = user
out["session_read_only_default"] = ro
out["read"] = {"products": n_products, "product_specs": n_specs, "chunks": n_chunks}
conn.rollback()


def attempt(label, sql, read_write):
    try:
        if read_write:
            cur.execute("SET TRANSACTION READ WRITE")
        cur.execute(sql)
        out[label] = "ALLOWED"
    except psycopg2.Error as e:
        out[label] = f"denied: {errorcodes.lookup(e.pgcode) if e.pgcode else type(e).__name__}"
    finally:
        conn.rollback()


attempt("update_products_default_session", "UPDATE products SET price = price WHERE false", False)
attempt("update_products_read_write", "UPDATE products SET price = price WHERE false", True)
attempt("delete_chunks_read_write", "DELETE FROM chunks WHERE false", True)
attempt("insert_documents_read_write",
        "INSERT INTO documents (product_id, document_type, title, content, content_hash) "
        "SELECT id, 'x', 'x', 'x', 'x' FROM products WHERE false", True)
attempt("select_documents", "SELECT count(*) FROM documents", False)
attempt("create_table_public_read_write", "CREATE TABLE public.consultant_write_probe (i int)", True)
attempt("create_temp_table_read_write", "CREATE TEMP TABLE consultant_write_probe (i int)", True)
conn.close()
print(json.dumps(out, indent=1))
