-- 006_indexes.sql
--
-- Indexes for the lookup/filter patterns the AI Consultant and ingestion
-- pipeline actually need. `(source, external_id)` on products, and the
-- other UNIQUE constraints declared in earlier migrations, already have
-- backing indexes automatically — this file only adds the rest.
--
-- No pgvector ANN index (ivfflat/hnsw) yet: the Samsung TV catalog is a
-- few hundred products, so a handful of thousand chunks at most. An exact
-- sequential scan over `chunks.embedding` at that scale is fast and always
-- returns exact nearest neighbours, whereas ivfflat/hnsw trade exactness
-- for speed and need a representative data distribution to tune well.
-- This is a deliberate decision, not an oversight — revisit once real
-- chunk volume is known (see docs/adr/001-catalog-rag-storage.md).
--
-- Idempotent: safe to re-run.

-- The UNIQUE constraint on (source, external_id) already backs lookups
-- that know the source; this covers lookups by external_id alone.
CREATE INDEX IF NOT EXISTS idx_products_external_id ON products (external_id);
CREATE INDEX IF NOT EXISTS idx_products_model_code ON products (model_code);
CREATE INDEX IF NOT EXISTS idx_products_product_url ON products (product_url);
CREATE INDEX IF NOT EXISTS idx_products_year ON products (year);
CREATE INDEX IF NOT EXISTS idx_products_screen_size ON products (screen_size_inches);
CREATE INDEX IF NOT EXISTS idx_products_panel_technology ON products (panel_technology);
CREATE INDEX IF NOT EXISTS idx_products_price ON products (price);
CREATE INDEX IF NOT EXISTS idx_products_is_available ON products (is_available);

CREATE INDEX IF NOT EXISTS idx_ingestion_errors_run_id ON ingestion_errors (run_id);

CREATE INDEX IF NOT EXISTS idx_product_specs_product_id ON product_specs (product_id);

CREATE INDEX IF NOT EXISTS idx_documents_product_id ON documents (product_id);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_product_id ON chunks (product_id);
