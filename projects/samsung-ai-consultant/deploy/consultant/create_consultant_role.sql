-- Phase 4D.2A: read-only, least-privilege role for the Samsung Consultant runtime.
--
-- Run against samsung_rag as the database owner, feeding this file on stdin after a line
--   \set password_verifier 'SCRAM-SHA-256$4096:...'
-- (a SCRAM verifier computed on the VPS: the plaintext password never reaches PostgreSQL, its
-- logs, a command line or this repository).
--
-- Grants follow what consultant/catalog_repository.py actually reads: products, product_specs,
-- chunks, and match_product_chunks() (SECURITY INVOKER; reads chunks + products). `documents` is
-- not read by the Consultant and is not granted. No INSERT/UPDATE/DELETE/TRUNCATE, no CREATE, no
-- ownership, no membership in samsung_ingestion / samsung_indexing / n8n.

\set ON_ERROR_STOP on

BEGIN;

CREATE ROLE samsung_consultant
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 5
    PASSWORD :'password_verifier';

-- Every session of this role is read-only with a bounded statement time, independent of the client.
ALTER ROLE samsung_consultant SET default_transaction_read_only = on;
ALTER ROLE samsung_consultant SET statement_timeout = '5s';
ALTER ROLE samsung_consultant SET idle_in_transaction_session_timeout = '30s';

GRANT CONNECT ON DATABASE samsung_rag TO samsung_consultant;
GRANT USAGE ON SCHEMA public TO samsung_consultant;
GRANT SELECT ON TABLE products, product_specs, chunks TO samsung_consultant;
GRANT EXECUTE ON FUNCTION match_product_chunks(vector, integer, integer, numeric, numeric, text, numeric,
                                               numeric, boolean) TO samsung_consultant;

COMMIT;
