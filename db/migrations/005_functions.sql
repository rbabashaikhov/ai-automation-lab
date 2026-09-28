-- 005_functions.sql
--
-- Vector similarity search over chunks, with optional filtering on the
-- structured product attributes an AI Consultant realistically needs to
-- narrow by (year, screen size range, panel technology, price range,
-- availability). Deliberately typed/named parameters rather than a
-- generic `filter jsonb` blob (the legacy `match_documents` approach):
-- typed parameters are self-documenting, let PostgreSQL validate argument
-- types at call time, and avoid building any dynamic SQL. This is not a
-- full hybrid search engine (no keyword/BM25 side yet) — see
-- docs/adr/001-samsung-rag-storage.md for what Phase 1 deliberately
-- leaves for a later phase.
--
-- Idempotent: safe to re-run (CREATE OR REPLACE).

CREATE OR REPLACE FUNCTION match_product_chunks(
    query_embedding             vector(1536),
    match_count                 integer DEFAULT 10,
    filter_year                 integer DEFAULT NULL,
    filter_min_screen_size      numeric DEFAULT NULL,
    filter_max_screen_size      numeric DEFAULT NULL,
    filter_panel_technology     text DEFAULT NULL,
    filter_min_price            numeric DEFAULT NULL,
    filter_max_price            numeric DEFAULT NULL,
    filter_is_available         boolean DEFAULT NULL
)
RETURNS TABLE (
    chunk_id      bigint,
    product_id    bigint,
    document_id   bigint,
    content       text,
    metadata      jsonb,
    similarity    double precision
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        c.id,
        c.product_id,
        c.document_id,
        c.content,
        c.metadata,
        1 - (c.embedding <=> query_embedding) AS similarity
    FROM chunks c
    JOIN products p ON p.id = c.product_id
    WHERE c.embedding IS NOT NULL
      AND (filter_year IS NULL OR p.year = filter_year)
      AND (filter_min_screen_size IS NULL OR p.screen_size_inches >= filter_min_screen_size)
      AND (filter_max_screen_size IS NULL OR p.screen_size_inches <= filter_max_screen_size)
      AND (filter_panel_technology IS NULL OR p.panel_technology = filter_panel_technology)
      AND (filter_min_price IS NULL OR p.price >= filter_min_price)
      AND (filter_max_price IS NULL OR p.price <= filter_max_price)
      AND (filter_is_available IS NULL OR p.is_available = filter_is_available)
    ORDER BY c.embedding <=> query_embedding
    LIMIT match_count;
$$;
