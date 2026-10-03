# ADR 002: RAG document/chunk construction design (Phase 3A)

## Status

Accepted (Phase 3A — deterministic document/chunk construction only; no
embeddings, no vector retrieval, no AI Agent yet).

## Context

Phase 1 shipped `documents`/`chunks` tables (see
`db/migrations/004_rag.sql`) and Phase 2 shipped a real 75-product
catalog with 4151 `product_specs` rows. Phase 3A has to decide, in code,
*what exactly gets embedded* before any embedding call happens in Phase
3B — this ADR records that decision and why, based on inspecting the
actual schema and the actual production corpus rather than assumptions.

Two schema facts constrain the design space, both confirmed by reading
`db/migrations/004_rag.sql` directly rather than assumed from memory:

- `documents.document_type` has `CHECK (document_type IN
  ('product_overview', 'specifications', 'comparison_context'))` and
  `UNIQUE (product_id, document_type)`. A product can have at most one
  row per type, and only these three types exist — chunking cannot
  invent a `document_type` per spec-group.
- `chunks` has no natural-key column beyond `UNIQUE (document_id,
  chunk_index)`. There is no schema-level "this is always the gaming
  chunk" identity.

## Decision 1: one document per product, `document_type = 'product_overview'`

Validated against the real corpus (median ~55 specs / ~2.7KB per
product, computed from the full 75-product corpus) rather than assumed:
a single `product_overview` document comfortably holds a product's full
typed attributes + every `product_specs` row without being unwieldy. A
second `specifications` document containing largely the same content
would duplicate `product_overview` rather than add retrieval value at
this data scale. `comparison_context` is inherently a cross-product
concept (comparing *multiple* products) that doesn't fit "one document
about one product" at all, and is explicitly out of scope until an AI
Agent (Phase 4) defines what a comparison document should contain.

**Alternative considered:** a `specifications` document per product
containing only the raw spec dump, separate from a shorter
`product_overview`. Rejected for Phase 3A — no evidence yet (no
evaluation run) that splitting improves retrieval, and it doubles
storage/re-indexing work for content that's currently identical. Revisit
if Phase 3B evaluation shows overview-level chunks under- or
over-retrieve relative to a pure-spec document.

## Decision 2: 7-section chunking, chunks replaced wholesale per rebuild

Chunk boundaries follow the 18 real `spec_group` values observed in the
corpus (see `indexing/README.md` "Corpus analysis"), folded into 7
semantic sections (`indexing/metadata.py:SPEC_GROUP_TO_SECTION`) matching
the task brief's suggested taxonomy (overview/display/gaming/audio/
smart_features/connectivity/physical_design) rather than either (a) one
chunk per raw `spec_group` (18 chunks/product — too fragmented; several
raw groups have 1-3 items and would produce near-empty chunks) or (b) one
chunk for the whole document (loses the "understandable/retrievable in
isolation" property the task brief asks for).

Because `chunks` has no natural key for "the gaming chunk of product X"
(see Context), and because db/README.md's own "Re-indexing" section
already documents "delete existing chunks and re-insert" as the intended
Phase 1 re-indexing pattern for exactly this reason, `repository.py`
deletes all of a document's chunks and re-inserts a fresh set on every
rebuild, rather than trying to diff/patch chunks in place. `chunk_index`
is assigned by always walking the same fixed `SECTION_ORDER`, so a given
section lands at the same index run-to-run *as a practical consequence*,
but nothing depends on that holding — the wholesale replacement makes
chunk-index stability a non-issue rather than a guarantee to maintain.
`metadata.section` (e.g. `"gaming"`) is the actual durable identity for a
logical chunk, carried in every row precisely so a smarter future
re-indexer (or Phase 3B's embedding-refresh logic) can match "the same
logical chunk" across rebuilds by section, not by index position.

**Alternative considered:** a synthetic uniqueness constraint/column
(e.g. `chunks.section TEXT` with `UNIQUE (document_id, section)`) to
make chunk identity schema-enforced instead of convention-enforced.
Rejected for Phase 3A as unnecessary complexity — it would require a new
migration (schema change), and wholesale replacement already gives every
property actually needed (idempotent, duplicate-free, no orphaned rows)
at the current data volume (~7 chunks/product × 75 products). Revisit if
a future phase needs to preserve a chunk's own `id`/embedding across
rebuilds when only *part* of a document changes (see Decision 3).

## Decision 3: change detection via per-chunk content hashing, not a skip-rebuild shortcut

`documents.content_hash` and each `chunks.content_hash` are SHA-256 over
the *built* text, always recomputed on every run (rebuilding is pure
string formatting — cheap, no network/API calls). This module does not
use Phase 2's `products.source_hash` as a shortcut to skip rebuilding
entirely; it treats `source_hash` changing as a strong *trigger* to
re-check, but always recomputes and only *compares* the result against
what's currently stored, because `source_hash`'s exact field set is not
formally guaranteed to stay in permanent lockstep with the document-text
field set as both evolve independently.

Per-chunk (not just per-document) hashing is what lets a future embedder
re-embed only the chunks whose *own* text actually changed — verified in
`tests/test_indexing_chunker.py::test_chunker_one_relevant_spec_change_only_affects_its_chunk`
and `tests/test_indexing_service.py::test_spec_change_is_detected_on_next_index`:
changing one gaming-section spec value changes only the `gaming` chunk's
hash; every other section's chunk hash is byte-identical before and
after. Given Decision 2's wholesale-replacement strategy, this doesn't
currently save a write (all chunks are still deleted/re-inserted every
rebuild) — its value is entirely for a future Phase 3B embedding step,
which can diff old vs. new per-chunk hashes to decide which chunks
actually need a new embedding call, without needing chunk `id` stability
across rebuilds.

## Decision 4: fields intentionally excluded from document/chunk content

- **`products.description`** (JSON-LD storefront text) is excluded
  entirely. Verified against all 75 production rows: it is uniformly
  SEO/marketing copy with emojis and an undecoded HTML entity (`&quot;`)
  — e.g. `"📱Купить Телевизор Samsung 50&quot; ... 🚚 ...
  ✅Гарантия. ✅Рассрочка или кредит."` — zero
  retrieval-differentiating product-characteristic content, and exactly
  the "invented marketing claim" text the task brief says RAG content
  must stay free of.
- **Five `product_specs` rows already restated from typed columns** in
  every document/chunk header ("Год выпуска", "Диагональ, дюйм",
  "Разрешение", "Технология экрана", "Частота обновления, Гц") are
  excluded from the spec-listing body only, to avoid stating the same
  fact twice in one document.

Nothing else is excluded — specs that happen to have an identical value
across all 75 *current* products (e.g. "Smart TV: Да") are kept, since
they are genuine product characteristics that could differentiate a
future catalog addition, per the task brief's "do not remove real
characteristics merely because they appear unimportant."

## Consequences

- A future Phase 3B only has to embed `chunks.content` for rows where
  `embedding IS NULL` or where a rebuild changed `content_hash` — no
  document-building logic needs to change to support that.
- Adding a `comparison_context` document type later (Phase 4, once the
  AI Agent defines what a cross-product comparison document should
  contain) is additive — it doesn't require revisiting this ADR's
  `product_overview` design.
- If future data reveals per-product spec counts far larger than the
  current ~36-75 range (e.g. a much richer future source), the 7-section
  chunking may need finer subdivision within a section (e.g. splitting
  `connectivity`'s `Интерфейсы` from `Беспроводная связь`) — the section
  taxonomy is a mapping table (`SPEC_GROUP_TO_SECTION`), not hardcoded
  logic, specifically so that's a data change, not a redesign.
