# Samsung RAG document/chunk indexing (Phase 3A)

Deterministic construction of `documents`/`chunks` rows from `products`/
`product_specs` — **no embeddings API calls, no `embedding` column
writes**. See [docs/adr/002-rag-document-chunking-design.md](../docs/adr/002-rag-document-chunking-design.md)
for the design decisions and their rationale, and the Phase 3A final
report for full-corpus statistics and the production integration test
result.

## Corpus analysis (75 production products, 2026-09-28)

```
products: 75, product_specs: 4151
specs/product: min=36 max=75 avg=55.3
```

18 distinct `spec_group` values, by frequency:

```
Интерфейсы 636, Изображение 438, Функции 358, Поддержка Smart TV 296,
Размеры и вес 289, Заводские данные 282, Беспроводная связь 276,
Игровой режим 270, Экран и разрешение 262, Звук 229,
Основные характеристики 225, Корпус 212, Настройка изображения 117,
Технологии приема сигнала 79, Комплектация 77, Электропитание 69,
Проекционная система 28, Поддерживаемые форматы 8
```

Notable findings from inspecting the real data before designing anything:

- No empty/null `spec_value` rows anywhere in the corpus (checked
  directly) — nothing to defensively strip.
- `products.description` (JSON-LD, carried over from Phase 2) is
  uniformly storefront marketing copy with emojis and an undecoded HTML
  entity — excluded entirely, see the ADR "Decision 4."
- A handful of spec_names are identical across all 75 products today
  (e.g. "Smart TV: Да", "Год выпуска: 2026") — kept anyway, since
  "currently uniform" isn't the same as "not a real characteristic."
- "Проекционная система" (28 rows) and "Поддерживаемые форматы" (8 rows)
  are rare groups present on only a subset of products — folded into
  `display`/`smart_features` respectively rather than given their own
  near-empty chunk type.

## Document model

**One document per product, `document_type = 'product_overview'`** (see
ADR "Decision 1" for why this is the schema-compatible, corpus-validated
choice — `documents.document_type` is schema-constrained to exactly
`product_overview` / `specifications` / `comparison_context`, read
directly from `db/migrations/004_rag.sql` rather than assumed).

Content = a typed-attribute header (name, model, series, category, year,
screen size, resolution, panel technology, refresh rate, price,
availability) + every `product_specs` row grouped by its original
`spec_group`, as deterministic `Name: value` lines. No LLM involved —
pure string formatting over already-structured data. See
`builder.py` module docstring for the exact, documented list of excluded
fields and why.

## Chunking strategy

7 semantic sections, derived from the 18 real `spec_group` values above
(`metadata.py:SPEC_GROUP_TO_SECTION`), matching the task brief's
suggested taxonomy:

| Section | Source spec_group(s) |
|---|---|
| `overview` | Основные характеристики (+ typed header) |
| `display` | Экран и разрешение, Изображение, Настройка изображения, Проекционная система |
| `gaming` | Игровой режим |
| `audio` | Звук |
| `smart_features` | Поддержка Smart TV, Функции, Поддерживаемые форматы |
| `connectivity` | Интерфейсы, Беспроводная связь, Технологии приема сигнала |
| `physical_design` | Корпус, Размеры и вес, Заводские данные, Комплектация, Электропитание |

`overview` is always present (it's the product's own typed identity).
Every other section is only emitted as a chunk if it has real spec
content for that product — no empty chunks. Every chunk restates a short
identity preamble (`Samsung <name>`, `Модель:`, `Категория:`, `Раздел:
<label>`) so it's understandable retrieved in isolation, without
repeating the full document. See ADR "Decision 2" for why chunks are
replaced wholesale per rebuild rather than diffed/patched in place.

## Chunk size statistics (measured over all 75 real products)

```
per-document: chars min=1679 max=3392 mean=2739 median=2666
              tokens (tiktoken cl100k_base) min=797 max=1649 mean=1291 median=1233
per-chunk:    chars min=222 max=940 mean=496 median=470
              tokens min=93 max=496 mean=229 median=219
chunks/product: min=6 max=7 mean=6.9 (gaming section present on 64/75 products)
```

Token counts use `tiktoken`'s `cl100k_base` encoding (the family
`text-embedding-3-small` uses) when installed — a local, offline
tokenizer call, not a network/embeddings API call — falling back to a
documented character-ratio approximation otherwise (see
`metadata.py:approx_token_count`). Max observed chunk size (496 tokens)
is far under `text-embedding-3-small`'s 8191-token limit; per the task
brief, chunk boundaries were chosen for semantic coherence, not to fill
that budget.

## Metadata

Every document and every chunk carries the same structured, filterable
fields (`metadata.py:build_structured_metadata`) — the exact typed
columns `match_product_chunks()` filters on (read directly from
`db/migrations/005_functions.sql`): `product_id`, `source`,
`external_id`, `model_code`, `product_url`, `year`, `series`,
`screen_size_inches`, `resolution`, `panel_technology`,
`refresh_rate_hz`, `price`, `currency`, `is_available`. Documents add
`document_type`/`spec_count`; chunks add `section`. No raw payloads or
large blobs are duplicated into metadata.

## Hashing / change detection

See ADR "Decision 3" for the full reasoning. Summary: `documents` and
every `chunks` row carry their own SHA-256 `content_hash`, always
recomputed on rebuild (cheap — pure formatting) and compared against
what's currently stored to report whether anything actually changed.
Per-chunk hashing (not just per-document) means a future embedder can
tell *which specific chunks* need a new embedding after a product
update — verified directly: changing one gaming-section spec value
changes only the `gaming` chunk's hash, and no other chunk's, in both a
pure unit test and a real-database service-level test.

## Database access

A third least-privilege Postgres role, `samsung_indexing` (see
`../README.md` "Database access"), scoped to `SELECT` on
`products`/`product_specs` and full CRUD on `documents`/`chunks` only —
never the `samsung_ingestion` or `n8n` role's own credential. Configured
via `INDEXING_DATABASE_URL` (see `.env.example`), separate from
ingestion's `DATABASE_URL`.

### `embedding` / `embedding_model` for Phase 3A rows

`embedding` is left `NULL` (omitted from every INSERT). `embedding_model`
is `NOT NULL DEFAULT 'text-embedding-3-small'` in the schema (no legal
NULL) — this module omits it too and lets the column default apply,
which is **not** a claim that an embedding exists. `embedding IS NULL` is
the only authoritative "no embedding yet" signal; see `repository.py`
module docstring.

## CLI

```bash
python -m indexing inspect
python -m indexing build --product-id 19
python -m indexing build --product-id 19 --render   # print full document + chunk text
python -m indexing build --limit 3 --dry-run
python -m indexing build                             # all products, persists
```

`inspect` and `--dry-run` never write to `documents`/`chunks`; `inspect`
requires `INDEXING_DATABASE_URL` (it reads `products`/`product_specs`
live) but performs no writes.

## Evaluation dataset

[`evaluation/retrieval_cases.json`](../evaluation/retrieval_cases.json) —
21 real-catalog retrieval cases (all `relevant_product_ids` verified
against actual production `model_code` values, none fabricated), spanning
`exact_lookup` (3), `structured_selection` (5), `semantic_feature_intent`
(6), `hybrid` (3), and `difficult_ambiguous` (4). Each case states an
`expected_mechanism` — `sql_sufficient` (7 cases), `vector_primary` (7),
`sql_plus_vector` (5), or `aggregate_not_retrieval` (2, superlative
questions like "the cheapest" / "the largest" that are MAX()/ORDER BY
questions, not similarity retrieval at all) — as a design prediction to
test against once Phase 3B produces real embeddings, not a claim that
retrieval has already been evaluated. Several cases are deliberately
grounded in already-discovered real-data quirks: the
`QE32LS03CBUXRU` name/year mismatch (Phase 2), the 11 products with no
gaming specs at all, and "Variable Refresh Rate" appearing only inside
another spec's multi-value text rather than as its own spec_name.

## Known limitations / open questions

- **No `specifications` or `comparison_context` documents yet** — see
  ADR "Decision 1." Revisit `specifications` if Phase 3B evaluation shows
  the combined `product_overview` document under/over-retrieves;
  `comparison_context` needs Phase 4's AI Agent to define its shape.
- **Section taxonomy is a snapshot of the current 18 `spec_group`
  values.** A future source or catalog change introducing new groups
  falls back to `physical_design` (see `metadata.py`) rather than being
  dropped, but a genuinely new category of specs (e.g. a "warranty
  options" group) might deserve its own section — revisit if
  `inspect`'s spec_group frequency table changes meaningfully.
- **Chunk identity is convention (fixed `SECTION_ORDER` + wholesale
  replacement), not schema-enforced** — see ADR "Decision 2" for why this
  is deliberately the simplest safe option today, and what would justify
  revisiting it.
- **`approx_token_count` falls back to a character-ratio approximation**
  if `tiktoken` isn't installed; the ratio (3.2 chars/token) was
  calibrated against real `tiktoken` counts on this corpus but isn't a
  universal constant for arbitrary text.
- **No retrieval has actually been evaluated yet** — `evaluation/retrieval_cases.json`
  is a baseline for Phase 3B, not a result. `expected_mechanism` values
  are design predictions.
