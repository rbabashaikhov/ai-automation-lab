# ADR 003: Embedding-aware chunk persistence (Phase 3B.1)

## Status

Accepted (Phase 3B.1 — fixes chunk persistence only; no schema change, no
embedding-generation logic changed, no full-catalog indexing).

## Context

Phase 3A's `IndexingRepository.replace_chunks` deleted every one of a
document's `chunks` rows and re-inserted a fresh set on every rebuild
(see ADR 002, "Decision 2"). That was a deliberate, documented choice
*at the time*, correct for a world where no chunk had an embedding yet —
but Phase 3B added a real embedding-generation step (the n8n `Samsung —
RAG Indexing` workflow) that writes `embedding`/`embedding_model` onto
existing rows. Once embeddings exist, delete-then-reinsert silently
discards them on every rebuild, even for a product whose content hasn't
changed at all — directly defeating Phase 3B's own stated incremental
goal ("unchanged chunk hash → existing embedding reused") and turning
every routine re-run of the Python builder into an unnecessary batch of
OpenAI calls at full-catalog scale.

## Decision: sync by logical section, never by array position

Each new chunk is matched against a document's *existing* chunks by
**`metadata->>'section'`** (e.g. `"gaming"`) — not row `id`, not
`chunk_index`, and not array position. This was already the intended
durable identity per ADR 002 ("`metadata.section`... is the actual
durable identity for 'this logical chunk'"); Phase 3B.1 is the first
persistence code to actually use it that way, rather than only carrying
it as descriptive metadata.

For each match:

| Existing section? | `content_hash` match? | Action |
|---|---|---|
| No | — | `INSERT`, `embedding` absent (NULL) |
| Yes | Same | `UPDATE chunk_index, metadata` only — the SQL statement never references `embedding`/`embedding_model` at all |
| Yes | Different | `UPDATE` content/hash/metadata, `embedding = NULL`, `embedding_model = DEFAULT` |
| Existing section absent from new build | — | `DELETE` |

`content_hash` — not `embedding IS NOT NULL` — is the sole authority for
whether an existing embedding still represents current content, per the
task brief's explicit requirement. This also correctly handles a chunk
that was already invalidated by a prior run and never re-embedded yet
(`embedding IS NULL`, `content_hash` unchanged since then): it's treated
as an ordinary "unchanged" match (row left alone, still NULL, still
correctly pending for n8n) rather than specially re-processed.

### Why row `id` is preserved "where practical" rather than always

An unchanged chunk keeps its exact row `id` (simple `UPDATE`, not
delete+insert). A *changed* chunk also keeps its row `id` — updating
content in place is simpler than delete+insert and there is no reason to
churn the id, but nothing downstream depends on that specifically (chunk
identity for matching purposes is the section, not the id). A *new*
chunk necessarily gets a new id (nothing to preserve). A *removed*
chunk's id is gone (the row is deleted, not archived) — Phase 1's schema
has no soft-delete/tombstone concept for chunks and adding one was
judged out of scope for this fix (see "Alternatives considered").

### Avoiding `UNIQUE (document_id, chunk_index)` collisions

`chunk_index` values are reassigned by walking the fixed `SECTION_ORDER`
and skipping empty sections (unchanged from ADR 002). If the *set* of
present sections changes, later sections' indices shift. Naively
`UPDATE`-ing rows to their new index in arbitrary order can transiently
collide with another still-live row's *current* index and violate the
unique constraint mid-transaction. Fixed without any schema change: every
surviving row whose index will change is first parked at a
guaranteed-unique negative placeholder (`chunk_index = -id`; real
indices are always `>= 0`), then every row is assigned its real final
index in a second pass. Verified directly: `tests/test_indexing_embedding_sync.py`
exercises both directions (a section reappearing, a section disappearing)
against a real disposable Postgres instance with real `UNIQUE` enforcement,
not mocked.

### Documents

`documents` already preserved its row `id` across rebuilds (its own
`ON CONFLICT (product_id, document_type) DO UPDATE`, unchanged since
Phase 3A). Phase 3B.1 adds one refinement: the `DO UPDATE` now carries a
`WHERE documents.content_hash IS DISTINCT FROM EXCLUDED.content_hash OR
documents.metadata IS DISTINCT FROM EXCLUDED.metadata` guard, so a
genuinely no-op rebuild doesn't even issue a real `UPDATE` — `updated_at`
stays untouched (verified in
`tests/test_indexing_embedding_sync.py::test_unchanged_rebuild_preserves_document_and_chunk_identity_and_embeddings`).
Documents have no `embedding` column of their own, so there was never an
embedding-loss risk at the document level — this refinement is purely
about not writing (and not firing `set_updated_at()`'s trigger) when
nothing actually changed.

## Alternatives considered

**A `chunks.section` column + `UNIQUE (document_id, section)` schema
constraint**, making logical identity enforced by the database instead
of by application code reading `metadata->>'section'`. Rejected for this
phase: it's a real schema change (a new column, backfill, a new unique
index), which the task brief requires stopping and explaining before
doing — and the read-then-compare approach in `sync_chunks` already gives
every property needed (idempotent, no duplicates, embeddings preserved,
verified against a real database) without it. Worth revisiting if a
future phase needs the database itself to reject a bug that writes two
chunks with the same section for one document — right now only this
module's own code path writes chunks at all, so that failure mode isn't
reachable in practice.

**Soft-delete / tombstone stale chunks** instead of `DELETE`, to preserve
some historical trace of a removed section. Rejected: nothing in this
project reads deleted chunks, `chunks.embedding` for a stale row would be
dead weight forever, and it would need its own schema change
(a `deleted_at` column) for no exercised benefit.

**Diff `metadata` field-by-field to decide whether to write it**, instead
of always writing `metadata` on every match (changed or unchanged
content). Rejected: `metadata` legitimately *can* change independent of
`content_hash` (e.g. `price`/`is_available` are in a chunk's metadata but
not necessarily in that chunk's own text for non-overview sections — see
`indexing/metadata.py:build_structured_metadata`), so unconditionally
refreshing metadata on every sync is correct, not wasteful; only
`embedding`/`embedding_model` needed the "don't touch if unchanged"
treatment, and the SQL for the unchanged path simply never mentions
those two columns.

## Consequences

- Re-running `python -m indexing build` on a product whose scraped
  content hasn't meaningfully changed is now a true no-op for every
  already-embedded chunk — verified against the real production
  `QE65S95HAUXPY` row: identical document `id`/`content_hash`/
  `updated_at`, and all 7 chunks' `id`, `content_hash`, `embedding`
  (byte-for-byte, via SHA-256 fingerprint of Postgres's own vector text
  rendering — never comparing/printing raw vectors), and `embedding_model`
  unchanged.
- A future full-catalog incremental indexing run can safely rely on
  `chunk_sync.invalidated` (now returned from
  `IndexingRepository.save_document_with_chunks` /
  `IndexingOutcome.chunk_sync`) to know exactly how many/which chunks
  actually need a fresh OpenAI call, rather than assuming "any rebuild
  might have touched anything."
- `sync_chunks` does one round trip to read existing chunks plus one
  statement per new chunk (compared to one bulk `execute_values` insert
  previously) — a deliberate trade of a small per-product overhead
  (~7 chunks) for correctness; not a concern at this catalog's scale (a
  few hundred chunks total), and nothing here changes the *number* of
  chunks per product or the chunking strategy itself (ADR 002 stands).
