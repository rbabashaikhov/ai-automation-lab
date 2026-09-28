-- 001_smoke_test_data.sql
--
-- Synthetic smoke-test dataset — NOT real Samsung product specs. Used by
-- db/test/run_local_tests.sh to exercise constraints, FK behaviour, and
-- pgvector similarity ordering against an isolated, disposable database.
-- Never apply this to the VPS / any shared database.
--
-- Each product's single chunk gets a synthetic "one-hot" embedding
-- (all zeros except a 1.0 at a distinct position) so similarity ordering
-- is exactly predictable without needing real embeddings.

INSERT INTO products (source, external_id, name, brand, year, series, screen_size_inches, panel_technology, price, currency, is_available, specs_text)
VALUES
    ('smoke_test', 'smoke-oled-1', 'Samsung OLED S95F 65"', 'Samsung', 2026, 'S95F', 65.0, 'OLED', 249990.00, 'RUB', true, 'Synthetic test specs for OLED.'),
    ('smoke_test', 'smoke-neoqled-1', 'Samsung Neo QLED QN90F 65"', 'Samsung', 2026, 'QN90F', 65.0, 'Neo QLED', 199990.00, 'RUB', true, 'Synthetic test specs for Neo QLED.'),
    ('smoke_test', 'smoke-qled-1', 'Samsung QLED Q60F 55"', 'Samsung', 2026, 'Q60F', 55.0, 'QLED', 99990.00, 'RUB', false, 'Synthetic test specs for QLED.')
ON CONFLICT (source, external_id) DO NOTHING;

INSERT INTO documents (product_id, document_type, title, content, content_hash)
SELECT p.id, 'product_overview', p.name, p.specs_text, 'smoke-hash-' || p.external_id
FROM products p
WHERE p.source = 'smoke_test'
ON CONFLICT (product_id, document_type) DO NOTHING;

-- One-hot embeddings: position 1 for OLED, position 2 for Neo QLED,
-- position 3 for QLED. All other components are 0.
INSERT INTO chunks (document_id, chunk_index, content, content_hash, embedding, embedding_model)
SELECT
    d.id,
    0,
    d.content,
    d.content_hash,
    (SELECT array_agg(
        CASE
            WHEN p.external_id = 'smoke-oled-1' AND i = 1 THEN 1.0
            WHEN p.external_id = 'smoke-neoqled-1' AND i = 2 THEN 1.0
            WHEN p.external_id = 'smoke-qled-1' AND i = 3 THEN 1.0
            ELSE 0.0
        END
        ORDER BY i
     )::vector FROM generate_series(1, 1536) AS i),
    'text-embedding-3-small'
FROM documents d
JOIN products p ON p.id = d.product_id
WHERE p.source = 'smoke_test'
ON CONFLICT (document_id, chunk_index) DO NOTHING;
