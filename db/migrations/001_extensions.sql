-- 001_extensions.sql
--
-- Foundational setup for the samsung_rag database. This must be run
-- against the `samsung_rag` database specifically, never against
-- `finance_tracker` or any other database on the shared PostgreSQL
-- instance. Creating the `samsung_rag` database itself is a one-time,
-- cluster-level operation that happens outside these migrations (see
-- db/README.md, "Bootstrapping a new environment") because CREATE DATABASE
-- cannot run inside the same session/transaction as the statements below.
--
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;

-- Shared trigger function used by every table below that carries an
-- `updated_at` column, so ingestion code never has to remember to set it.
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
