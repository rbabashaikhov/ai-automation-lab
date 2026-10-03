-- 004_rag.sql
--
-- RAG document/chunk model. A product can have more than one document
-- (product_overview, specifications, comparison_context, ...), each
-- document has at most one *current* version per type (re-ingestion
-- updates it in place rather than accumulating duplicates — see
-- db/README.md, "Re-indexing"), and each document is split into chunks
-- that carry the actual embeddings.
--
-- Embedding model / dimension: OpenAI text-embedding-3-small, vector(1536).
-- This matches the legacy SuperRAG Agent workflow's "Embeddings OpenAI1"
-- node and its `match_documents` function's `vector(1536)` column, so it
-- is a continuation of an already-made decision, not a new one. Changing
-- it later requires a new migration that rebuilds the embedding column
-- and re-embeds every chunk — it is not a config toggle.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS documents (
    id              BIGSERIAL PRIMARY KEY,
    product_id      BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    document_type   TEXT NOT NULL DEFAULT 'product_overview'
                        CHECK (document_type IN ('product_overview', 'specifications', 'comparison_context')),
    title           TEXT,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_documents_product_type UNIQUE (product_id, document_type)
);

DROP TRIGGER IF EXISTS trg_documents_set_updated_at ON documents;
CREATE TRIGGER trg_documents_set_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS chunks (
    id                BIGSERIAL PRIMARY KEY,
    document_id       BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    -- Denormalized from documents.product_id (kept in sync by
    -- trg_chunks_sync_product_id below) purely so filtered vector search
    -- can join/filter on chunks without a join to documents on every call.
    product_id        BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    chunk_index       INTEGER NOT NULL,
    content           TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding         vector(1536),
    embedding_model   TEXT NOT NULL DEFAULT 'text-embedding-3-small',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_chunks_document_index UNIQUE (document_id, chunk_index)
);

-- Removes the need for ingestion code to look up/pass product_id
-- correctly on every chunk insert, and makes the denormalized column
-- impossible to desync from its source of truth (documents.product_id).
CREATE OR REPLACE FUNCTION sync_chunk_product_id()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_product_id BIGINT;
BEGIN
  SELECT product_id INTO v_product_id FROM documents WHERE id = NEW.document_id;
  IF v_product_id IS NULL THEN
    RAISE EXCEPTION 'chunks.document_id % does not reference an existing document', NEW.document_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  NEW.product_id := v_product_id;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_chunks_sync_product_id ON chunks;
CREATE TRIGGER trg_chunks_sync_product_id
    BEFORE INSERT OR UPDATE OF document_id ON chunks
    FOR EACH ROW
    EXECUTE FUNCTION sync_chunk_product_id();
