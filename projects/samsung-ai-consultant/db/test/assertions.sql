-- assertions.sql
--
-- Run after migrations + db/seed/001_smoke_test_data.sql against a
-- disposable test database. Every check RAISEs EXCEPTION on failure, so a
-- single `psql -v ON_ERROR_STOP=1 -f assertions.sql` fails loudly and
-- non-zero on the first broken assumption.

-- 1) All expected tables exist.
DO $$
DECLARE
    missing TEXT;
BEGIN
    SELECT string_agg(t, ', ') INTO missing
    FROM unnest(ARRAY['ingestion_runs', 'ingestion_errors', 'products',
                       'product_specs', 'documents', 'chunks']) AS t
    WHERE to_regclass('public.' || t) IS NULL;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'Missing expected table(s): %', missing;
    END IF;
    RAISE NOTICE 'OK: all expected tables exist';
END $$;

-- 2) pgvector extension is available.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'pgvector extension is not installed';
    END IF;
    RAISE NOTICE 'OK: pgvector extension installed';
END $$;

-- 3) CHECK constraint on ingestion_runs.status is enforced.
DO $$
BEGIN
    BEGIN
        INSERT INTO ingestion_runs (source, status) VALUES ('assertions_test', 'not_a_real_status');
        RAISE EXCEPTION 'ingestion_runs.status CHECK constraint did not fire';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'OK: ingestion_runs.status CHECK enforced';
    END;
END $$;

-- 4) UNIQUE constraint on products (source, external_id) is enforced.
DO $$
BEGIN
    BEGIN
        INSERT INTO products (source, external_id, name) VALUES ('assertions_test', 'dup-1', 'Dup A');
        INSERT INTO products (source, external_id, name) VALUES ('assertions_test', 'dup-1', 'Dup B');
        RAISE EXCEPTION 'products (source, external_id) UNIQUE constraint did not fire';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'OK: products (source, external_id) UNIQUE enforced';
    END;
END $$;

-- 5) FK from chunks.document_id is enforced.
DO $$
BEGIN
    BEGIN
        INSERT INTO chunks (document_id, chunk_index, content, content_hash, embedding_model)
        VALUES (-1, 0, 'orphan', 'orphan-hash', 'text-embedding-3-small');
        RAISE EXCEPTION 'chunks.document_id FK constraint did not fire';
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'OK: chunks.document_id FK enforced';
    END;
END $$;

-- 6) Deleting a product cascades to its documents and chunks.
DO $$
DECLARE
    v_product_id BIGINT;
    v_document_id BIGINT;
BEGIN
    INSERT INTO products (source, external_id, name)
    VALUES ('assertions_test', 'cascade-1', 'Cascade Test')
    RETURNING id INTO v_product_id;

    INSERT INTO documents (product_id, document_type, content, content_hash)
    VALUES (v_product_id, 'product_overview', 'cascade content', 'cascade-hash')
    RETURNING id INTO v_document_id;

    INSERT INTO chunks (document_id, chunk_index, content, content_hash, embedding_model)
    VALUES (v_document_id, 0, 'cascade chunk', 'cascade-chunk-hash', 'text-embedding-3-small');

    DELETE FROM products WHERE id = v_product_id;

    IF EXISTS (SELECT 1 FROM documents WHERE id = v_document_id) THEN
        RAISE EXCEPTION 'documents row survived product delete (cascade not working)';
    END IF;
    IF EXISTS (SELECT 1 FROM chunks WHERE document_id = v_document_id) THEN
        RAISE EXCEPTION 'chunks row survived product delete (cascade not working)';
    END IF;
    RAISE NOTICE 'OK: ON DELETE CASCADE products -> documents -> chunks works';
END $$;

-- 7) chunks.product_id stays in sync with documents.product_id (trigger).
DO $$
DECLARE
    v_expected BIGINT;
    v_actual BIGINT;
BEGIN
    SELECT d.product_id, c.product_id INTO v_expected, v_actual
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    JOIN products p ON p.id = d.product_id
    WHERE p.source = 'smoke_test'
    LIMIT 1;

    IF v_expected IS NULL THEN
        RAISE EXCEPTION 'smoke_test seed data not found; run db/seed/001_smoke_test_data.sql first';
    END IF;
    IF v_expected != v_actual THEN
        RAISE EXCEPTION 'chunks.product_id (%) out of sync with documents.product_id (%)', v_actual, v_expected;
    END IF;
    RAISE NOTICE 'OK: chunks.product_id sync trigger works';
END $$;

-- 8) vector(1536) accepts embeddings and similarity search returns rows.
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT count(*) INTO v_count
    FROM match_product_chunks(
        (SELECT embedding FROM chunks c JOIN documents d ON d.id = c.document_id
         JOIN products p ON p.id = d.product_id WHERE p.external_id = 'smoke-oled-1'),
        10
    );
    IF v_count = 0 THEN
        RAISE EXCEPTION 'match_product_chunks returned no rows for smoke-test data';
    END IF;
    RAISE NOTICE 'OK: match_product_chunks returns rows (% found)', v_count;
END $$;

-- 9) Vector retrieval returns the expected nearest-neighbour order.
-- Query vector is closest to the OLED product's one-hot embedding.
DO $$
DECLARE
    v_top_external_id TEXT;
    v_query vector(1536);
BEGIN
    SELECT array_agg(CASE WHEN i = 1 THEN 0.9 WHEN i = 2 THEN 0.05 ELSE 0.0 END ORDER BY i)::vector
    INTO v_query
    FROM generate_series(1, 1536) AS i;

    SELECT p.external_id INTO v_top_external_id
    FROM match_product_chunks(v_query, 1) m
    JOIN products p ON p.id = m.product_id
    ORDER BY m.similarity DESC
    LIMIT 1;

    IF v_top_external_id IS DISTINCT FROM 'smoke-oled-1' THEN
        RAISE EXCEPTION 'Expected smoke-oled-1 to rank first, got: %', v_top_external_id;
    END IF;
    RAISE NOTICE 'OK: vector similarity ordering matches expected nearest neighbour';
END $$;

-- 10) match_product_chunks filtering by structured product attributes works.
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT count(*) INTO v_count
    FROM match_product_chunks(
        (SELECT embedding FROM chunks c JOIN documents d ON d.id = c.document_id
         JOIN products p ON p.id = d.product_id WHERE p.external_id = 'smoke-oled-1'),
        10,
        filter_panel_technology => 'QLED'
    );
    IF v_count != 1 THEN
        RAISE EXCEPTION 'Expected exactly 1 QLED-filtered match, got %', v_count;
    END IF;
    RAISE NOTICE 'OK: match_product_chunks structured-attribute filtering works';
END $$;
