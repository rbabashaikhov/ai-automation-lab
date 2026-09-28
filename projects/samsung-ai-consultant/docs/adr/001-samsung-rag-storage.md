# ADR 001: Samsung RAG storage architecture

## Status

Accepted (Phase 1 — schema only; no ingestion pipeline or AI Agent yet).

## Context

The Samsung TV AI Consultant is being rebuilt from a legacy prototype. Two
legacy n8n artifacts exist and were inspected directly (via the read-only
`n8n_tool` CLI in this repository) as part of this decision, rather than
assumed from memory:

- **`Parsing`** (`workflows/legacy/parsing.sanitized.json`) — scrapes a Samsung TV
  catalog site, extracting per-product `id`, `mpn`, `sku`, `name`, `brand`,
  `category`, `price`, `salePrice`, `currency`, `stock`, `url`, `image`,
  and a free-text `specs_text`, and writes them to Google Sheets.
- **`SuperRAG Agent`** (`workflows/legacy/superrag-agent.sanitized.json`) — the
  generic n8n "chat with your Google Drive files" community template,
  adapted with a Samsung-persona system prompt. It ingests arbitrary
  PDF/CSV/Excel/Docs files from Google Drive into Supabase Postgres:
  - `documents(id, content, metadata jsonb, embedding vector(1536))` +
    a `match_documents(query_embedding, match_count, filter jsonb)`
    function using pgvector cosine distance (`<=>`).
  - `document_metadata(id text PK, title, url, created_at, schema)` — one
    row per ingested *file*.
  - `document_rows(id, dataset_id -> document_metadata.id, row_data
    jsonb)` — one row per spreadsheet row of a tabular file, queried via
    arbitrary agent-generated SQL against `row_data->>'column'`.
  - Embeddings: OpenAI `text-embedding-3-small`. Chat model: OpenRouter
    `openai/gpt-4o`.

This is a well-built, general-purpose prototype — the "any file, any
shape, ask an LLM to write SQL against it" pattern is a reasonable
starting point for exploring a dataset interactively. It remains useful
as a reference implementation, and this ADR does not read as a criticism
of it. But it has three properties that make it the wrong foundation to
build a production-like, domain-specific consultant on:

1. **No product entity.** A "dataset" is a Google Drive file. There is no
   row that means "this Samsung TV." Comparing two TVs' screen sizes
   means locating the right file, then parsing `row_data->>'screen_size'`
   out of JSONB, for every query.
2. **No lifecycle.** Nothing tracks whether a product is still in the
   catalog, when it was first/last seen, or whether it's in stock.
3. **Supabase-shaped.** `vectorStoreSupabase` nodes and Supabase's
   `service_role`-style access pattern are baked into the ingestion side.
   Supabase is no longer part of the target architecture (see Decision).

## Decision

Build a **domain-oriented, typed PostgreSQL + pgvector schema**, not a
literal continuation of `documents` / `document_rows` / `document_metadata`,
and **not on Supabase** — self-hosted PostgreSQL + pgvector only, on the
VPS instance already running n8n.

Concretely (full detail in `db/README.md`):

- `products` is a first-class canonical entity with typed columns for
  every attribute the AI Consultant needs to filter or compare on (year,
  screen size, panel technology, refresh rate, price, availability, ...).
- `product_specs` holds long-tail/non-standard characteristics as
  `(spec_group, spec_name, spec_key, spec_value, ...)` rows — the one
  place this schema *does* keep the "flexible key/value" spirit of the
  legacy model, but scoped to genuinely variable long-tail data rather
  than used for every field.
- `extra_attributes` / `raw_payload` JSONB columns on `products` cover
  whatever doesn't fit the above, and preserve the original scraped
  payload for debugging/audit — JSONB is used deliberately, not avoided
  outright.
- `documents` / `chunks` keep the RAG document/chunk shape from the
  legacy `documents` table, but `chunks.product_id` (kept in sync by
  trigger) ties every retrievable chunk back to a real product, and
  `chunks.embedding_model` is stored per row rather than only implied by
  a single global column dimension.
- `ingestion_runs` / `ingestion_errors` give ingestion its own history and
  audit trail, instead of ingestion-time signals leaking onto the
  canonical product row (see `db/README.md`, "Where does parser/ingestion
  state live?").
- Embedding model and dimension (`text-embedding-3-small`, `vector(1536)`)
  are **kept** from the legacy workflow — that part of the legacy design
  already works and changing it is an unrelated, expensive decision
  (re-embedding everything), not something this schema phase should
  bundle in.

## Alternatives considered

**Supabase, continued.** Rejected per explicit target architecture:
self-hosted PostgreSQL + pgvector on infrastructure already operated
(the same VPS, same PostgreSQL instance, new `samsung_rag` database).
Supabase would add an external managed dependency, a second place secrets
live, and API-key-shaped access control this project doesn't need when
the AI Agent already talks to Postgres directly via n8n's Postgres nodes.

**Universal `document_rows`-only model, continued as-is.** Rejected
because it optimizes for "ingest anything, ask anything" generality at
the cost of the one thing a production TV consultant needs most: fast,
correct, typed filtering ("OLED, 65 inch, under 250000₽, in stock").
Every such query would otherwise require casting JSONB on every row, with
no indexes that mean anything domain-wise, and no enforced identity for
what a "product" even is (a dataset row is only unique within one
uploaded file, not across the catalog).

## Consequences

- A future ingestion phase writes to a schema that already knows what a
  product, a document, and a chunk are — it only has to map scraped
  fields onto typed columns, decide `spec_key` slugs for long-tail specs,
  and manage lifecycle (`first_seen_at`/`last_seen_at`/`is_available`).
- The AI Agent's future SQL tool(s) can query `products` directly with
  ordinary typed `WHERE` clauses instead of `row_data->>'x'` casts, and
  `match_product_chunks` gives it a single, safe (no dynamic SQL) vector
  search primitive with structured-attribute filtering built in.
- No hybrid (vector + keyword) search engine yet, and no ANN index yet —
  both deliberately deferred; see `db/README.md`, "Indexing strategy" and
  "Embedding model / vector search," for why and what would trigger
  revisiting each.
- `sku`/`model_code` are kept as informational, indexed columns but are
  explicitly **not** the canonical identity — `(source, external_id)` is,
  because that is the one field the legacy pipeline already trusted for
  deduplication. This is a documented, revisitable assumption, not a
  verified guarantee that `mpn`/model codes are unusable; a future
  ingestion phase that crawls the full catalog may find they are reliable
  enough to become a secondary uniqueness guarantee.
