-- 003_products.sql
--
-- Canonical Samsung TV catalog. Domain-oriented, typed model (see
-- docs/adr/001-samsung-rag-storage.md) rather than the legacy SuperRAG
-- universal `document_rows(row_data jsonb)` pattern: fields the AI
-- Consultant actually needs to filter/compare on (year, screen size,
-- panel technology, price, availability, ...) are typed columns; anything
-- long-tail or non-standard lives in `extra_attributes` / `product_specs`.
--
-- Canonical identity: (source, external_id). `external_id` is populated
-- from the source site's own internal product id (the same field the
-- legacy `Parsing` workflow already used as its Google Sheets matching
-- column — see db/README.md, "Canonical product identity" for the full
-- reasoning and the documented uncertainty around `sku`/`model_code`).
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS products (
    id                   BIGSERIAL PRIMARY KEY,

    source               TEXT NOT NULL,
    external_id          TEXT NOT NULL,
    sku                  TEXT,
    model_code           TEXT,
    name                 TEXT NOT NULL,
    brand                TEXT NOT NULL DEFAULT 'Samsung',
    product_url          TEXT,
    category             TEXT,

    year                 INTEGER,
    series               TEXT,

    screen_size_inches   NUMERIC(4,1),
    resolution           TEXT,
    panel_technology     TEXT,
    refresh_rate_hz      INTEGER,

    price                NUMERIC(12,2),
    sale_price           NUMERIC(12,2),
    currency             TEXT NOT NULL DEFAULT 'RUB',
    stock_quantity       INTEGER,

    is_available         BOOLEAN NOT NULL DEFAULT true,

    description          TEXT,
    specs_text           TEXT,

    source_hash          TEXT,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    extra_attributes     JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_payload          JSONB,

    CONSTRAINT uq_products_source_external_id UNIQUE (source, external_id)
);

DROP TRIGGER IF EXISTS trg_products_set_updated_at ON products;
CREATE TRIGGER trg_products_set_updated_at
    BEFORE UPDATE ON products
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- Long-tail / non-normalized characteristics that don't earn a typed
-- column on `products`. `spec_key` is a stable, machine-generated slug
-- (e.g. "hdr_formats"); `spec_name` is the human-readable label as scraped
-- (may be localized, e.g. Russian). Generating `spec_key` from `spec_name`
-- is an ingestion-phase concern, not a schema concern (see db/README.md).
CREATE TABLE IF NOT EXISTS product_specs (
    id                 BIGSERIAL PRIMARY KEY,
    product_id         BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    spec_group         TEXT,
    spec_name          TEXT NOT NULL,
    spec_key           TEXT NOT NULL,
    spec_value         TEXT,
    normalized_value    TEXT,
    unit               TEXT,
    sort_order         INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_product_specs_product_key UNIQUE (product_id, spec_key)
);
