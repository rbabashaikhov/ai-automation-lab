-- 002_ingestion.sql
--
-- Ingestion run history and error log. These tables are ingestion-process
-- metadata, deliberately separate from the canonical `products` table (see
-- docs/adr/001-samsung-rag-storage.md, "Where does ingestion state live?").
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id                   BIGSERIAL PRIMARY KEY,
    source               TEXT NOT NULL,
    source_url           TEXT,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at          TIMESTAMPTZ,
    status               TEXT NOT NULL DEFAULT 'running'
                             CHECK (status IN ('running', 'success', 'partial', 'failed')),
    products_discovered  INTEGER NOT NULL DEFAULT 0,
    products_inserted    INTEGER NOT NULL DEFAULT 0,
    products_updated     INTEGER NOT NULL DEFAULT 0,
    products_failed      INTEGER NOT NULL DEFAULT 0,
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS ingestion_errors (
    id                    BIGSERIAL PRIMARY KEY,
    run_id                BIGINT NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
    product_external_id   TEXT,
    product_url           TEXT,
    stage                 TEXT NOT NULL,
    error_code            TEXT,
    error_message         TEXT NOT NULL,
    raw_payload           JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
