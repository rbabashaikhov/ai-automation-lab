# Catalog ingestion pipeline (Phase 2)

Ingests the GalaxyStore Samsung TV catalog into the Phase 1 PostgreSQL
schema (`db/migrations/`). See the top-level
[README.md](../README.md) for project status and
[docs/adr/001-catalog-rag-storage.md](../docs/adr/001-catalog-rag-storage.md)
for the schema decision this ingests into. The extractors below are specific to
this source; the operator's refresh procedure (ingestion → indexing →
embeddings) is in the project README, "Updating the catalog".

## Source

```
https://galaxystore.ru/catalog/televizory/year=2026/
```

Configurable via `SAMSUNG_CATALOG_URL` — nothing in the code hardcodes
`2026`. Note from discovery: not every product returned by this
year-filtered catalog URL is actually a 2026 model (an older `2023` "The
Frame" TV appeared in a real listing page); `year` is always read from the
product's own specifications (`SM_PARAMS.product.specifications`), never
assumed from the catalog URL or filter.

## Extraction cascade

Four structured sources, checked in this order, each independently
identifiable as `extraction_source` in `ExtractedProduct.extraction_sources`
and in the raw payload stored in `products.raw_payload`:

1. **`json_ld`** — `<script type="application/ld+json">` `Product` /
   `BreadcrumbList` blocks. Source of the real manufacturer SKU
   (`sku`), name, price/currency, and `offers.availability`.
2. **`digital_data`** — `window.digitalData`. Primary source for
   `external_id` (canonical identity), category, stock, and price.
3. **`sm_params`** — `SM_PARAMS.catalog` (listing pages) /
   `SM_PARAMS.product` (detail pages). Primary source for the full
   specification tree (`specifications.grouped`) that
   `product_specs` and the typed spec columns are built from, plus the
   catalog's `articleMain` series hint.
4. **`html`** — last-resort regex fallback (`<link rel="next">`,
   `og:image`, `<title>`, meta description). Never re-derives the spec
   table from markup — `sm_params` already gives a clean structured
   version of that data.

This differs from the legacy `Parsing` workflow's assumptions in two
verified ways (both re-checked against the live site in 2026-09, not
assumed from the legacy code):

- The legacy code looked for a single `window.SM_PARAMS = {..., catalog:
  {...}}` object literal. The live site instead assigns each section as
  its own statement (`SM_PARAMS.catalog = {...};`,
  `SM_PARAMS.product = {...};`), which this pipeline targets directly.
- A naive non-greedy `\{.*?\};` regex (as the legacy Code node used)
  breaks on real payloads because nested string values themselves contain
  `};`-shaped substrings. `extractors/__init__.py:extract_balanced_json`
  does real brace-depth matching instead.

## Field precedence

Documented in full in `ingestion/product.py`'s module docstring. Summary:

| Field | Precedence |
|---|---|
| `external_id` | `digitalData.product.id` only (no fallback — legacy's trusted identity field) |
| `model_code` (mpn) | `digitalData.mpnCode` → JSON-LD `sku` → `SM_PARAMS` `serialCode` → catalog hint |
| `products.sku` | Always `NULL` — see "sku is deliberately left NULL" below |
| `name` | `digitalData.name` (entity-decoded) → JSON-LD `name` → catalog hint |
| `price` / `sale_price` | `digitalData.unitPrice` / `unitSalePrice` → catalog-page prices |
| `is_available` | JSON-LD `offers.availability` → `SM_PARAMS canBuy` → `stock > 0` → default `true` |
| `year`, `series`, `screen_size_inches`, `resolution`, `panel_technology`, `refresh_rate_hz` | `SM_PARAMS.product.specifications.grouped` (see `normalize.py`) |

### `sku` is deliberately left `NULL`

Verified during discovery (`tests/test_extractors_digital_data.py::test_listing_internal_sku_code_equals_id_not_a_real_sku`):
`digitalData`'s `skuCode` is always numerically equal to its own `id` —
e.g. `{"id": "3965052", "skuCode": "3965052", ...}`. It is not a real
manufacturer SKU, just a copy of the site's internal listing id. The
legacy `Parsing` workflow copied this value into its own `sku` field,
which is exactly the mistake Phase 1's ADR flagged as "documented,
revisitable." This ingestion pipeline does not repeat it: `products.sku`
is populated only if a genuine manufacturer SKU source is found in a
future phase; for now it stays `NULL` rather than silently carrying
forward a misleading legacy value.

## Normalization

`ingestion/normalize.py`. The GalaxyStore spec tree already gives target
values as clean, near-final strings (e.g. `"Диагональ, дюйм": ["50"]`,
`"Частота обновления, Гц": ["60"]`, `"Год выпуска": ["2026"]`), so
normalization is mostly locating the right spec by its (Russian, exact
match) name and light type coercion — not free-text unit parsing.
`normalize_resolution` is the one exception: it parses an explicit
`WxH` pixel pattern, falling back to a small fixed table of unambiguous
industry-standard resolution labels (`4K Ultra HD` → `3840x2160`, etc.)
only when no explicit dimensions are present. `series` prefers the
catalog's `articleMain` hint (e.g. `"M70"`) and falls back to a
documented, revisitable regex over the model code
(`UE50M70HAUXPY` → `M70`) when no catalog hint is available (e.g.
`fetch-product` run directly on a URL).

## Validation

`ingestion/validate.py`. A product must have a non-empty `external_id`,
`name`, and `product_url`; brand must be Samsung; and it must have either
≥3 `product_specs` rows or substantial `specs_text`. Validation never
invents data to pass — a failing product is recorded in
`ingestion_errors` (stage `validate`) and skipped, not partially written.

## Persistence

`ingestion/repository.py`, against the existing Phase 1 schema — no
schema changes. `UPSERT ... ON CONFLICT (source, external_id)` keeps
`first_seen_at` fixed (never in the `UPDATE SET` list) while always
bumping `last_seen_at`. `product_specs` are replaced wholesale
(delete-then-bulk-insert) per product per run — the simplest strategy
that is provably duplicate-free at this data volume.

**Deactivation is deferred.** This phase does not implement "mark
products absent from a complete crawl as `is_available = false`". Each
upsert keeps a re-scraped product's own availability accurate, but no
function here scans for and deactivates products missing from a run —
see "Known limitations" below.

## Source hash

`normalize.compute_source_hash` — SHA-256 over a canonical JSON of only
content-bearing fields (name, brand, category, year, series, screen
size, resolution, panel technology, refresh rate, price, sale price,
currency, availability, description, specs_text). Deliberately excludes
anything timestamp-like or unstable-per-request (exact stock count,
review counts, image CDN cache paths, page generation timestamps) so
re-scraping an unchanged product yields an identical hash — the point is
letting a future Phase 3 skip re-embedding/re-indexing products that
haven't meaningfully changed.

## HTTP / retry policy

`ingestion/http.py`. Explicit timeout (default 20s), a descriptive
User-Agent, and exponential backoff retries (default up to 4 retries,
1s/2s/4s/8s) for `429/500/502/503/504`. A `404` is never retried. A
configurable polite delay (default 2s) is enforced before every request,
sequentially — ingestion is single-threaded by design.

## Catalog discovery / pagination

`ingestion/catalog.py`. Pagination is driven primarily by
`digitalData.listing.currentPage`/`pagesCount` (confirmed present on
every catalog page), cross-checked against `<link rel="next">`. Visited
URLs are tracked to stop pagination loops, and `--max-pages` caps page
count regardless. Pagination metadata (`CatalogPage.current_page` /
`.pages_count` / `.next_url`) is represented separately from the product
list — never injected as a synthetic `_meta` product the way the legacy
workflow did. Products are deduplicated by URL across pages.

## CLI

```bash
python -m ingestion discover --max-pages 1
python -m ingestion fetch-product <url>
python -m ingestion run --max-pages 1 --limit-products 3 --dry-run
python -m ingestion run
```

`--dry-run` performs extraction/normalization/validation and prints a
summary, but **never writes to PostgreSQL at all** — no `products` /
`product_specs` writes, and also no `ingestion_runs` / `ingestion_errors`
row, since those tables record what an ingestion *execution* did and a
dry-run doesn't execute one. `discover` and `fetch-product` never require
`DATABASE_URL`.

## Configuration

`ingestion/config.py`, `.env.example`. Requires `DATABASE_URL` for `run`
(not for `discover`/`fetch-product`, and not for `run --dry-run`). Never
commit the real `.env` (git-ignored at the repo root).

## Full-catalog production run (2026-09-28)

The complete configured catalog (3 pages, 75 listing entries) was crawled
twice against the real `samsung_rag` database, via the `samsung_ingestion`
role over an SSH tunnel, at the default conservative settings (2s delay,
up to 4 retries). Both runs: **75 discovered, 75 processed, 0 failed, 0
ingestion_errors, status `success`**. Run 1: 75 inserted / 0 updated. Run
2 (idempotency check, no fixes applied in between — nothing needed
fixing): 0 inserted / 75 updated, confirming:

- `products` row count stayed at 75 (no duplication).
- `product_specs` row count stayed at 4151 (no duplication); zero
  products have a duplicate `spec_key`.
- `first_seen_at` was byte-identical before/after run 2 for every product.
- `last_seen_at` advanced for every product after run 2.
- `ingestion_runs` recorded both executions independently with correct
  per-run counters.

Final `samsung_rag` state: **75 `products` rows, 4151 `product_specs`
rows** — these are real, successfully-parsed, currently-retained
production data (not cleaned up, per the production-run authorization).

### Distributions (75 products)

- **`year`**: all 75 → `2026`. See "The `QE32LS03CBUXRU` name/year
  mismatch" below for why this is correct, not a bug.
- **`panel_technology`**: Mini LED 17, Neo QLED 16, OLED 15, LED 11,
  Micro RGB 9, QLED 6, Micro LED 1.
- **`screen_size_inches`**: spans 27"–115" across 17 distinct sizes (most
  common: 65" ×13, 55" ×12, 75" ×10).
- **`resolution`**: `3840x2160` (4K) 68, `1920x1080` (FHD) 4,
  `2560x1440` 1, `4968x2808` 1, `1366x768` 1 — see "Non-4K/FHD resolution
  outliers" below.
- **`refresh_rate_hz`**: 120 Hz ×41, 60 Hz ×27, 50 Hz ×7.
- **`is_available`**: `true` 66, `false` 9 (all from `SM_PARAMS`
  `canBuy`/stock signals — see `normalize_availability`, no manual
  overrides).
- **spec-row count per product**: ranges 36–75, median ~54; no product
  fell anywhere near the validation floor of 3.

### Null/missing rates

Every typed field populated on 75/75 products **except**: `sku` (75/75
`NULL` — deliberate, see "`sku` is deliberately left `NULL`" above) and
`sale_price` (44/75 `NULL` — no active discount on those products, not a
gap: `price`/`sale_price` extraction only ever fills `sale_price` when
the source gives a real, lower discounted price).

### Duplicate checks (all clean, 75/75)

`(source, external_id)`: 0 duplicates. `model_code`: 0 duplicates, 0
`NULL`s. `product_url`: 0 duplicates.

### Full-catalog MPN/model-code analysis

Checked cross-source consistency for all 75 products by comparing
`digitalData.mpnCode`, JSON-LD `offers` block's `sku`, and
`SM_PARAMS.product.main.serialCode` (all three stored independently in
`raw_payload`): **0 mismatches, 0 missing values, out of 75.**
`model_code` is 100% non-null and 100% unique across the complete
catalog. This is strong, now full-catalog evidence that `model_code`
could become a secondary uniqueness guarantee — **but, per explicit
instruction, this phase does not change the canonical identity or the
`UNIQUE (source, external_id)` constraint.** That remains a decision for
the schema owner to make deliberately, with this evidence in hand, not
something ingestion code changes unilaterally.

### The `QE32LS03CBUXRU` name/year mismatch

Flagged during discovery as an apparent "non-2026 product under the
`year=2026` filter": its product **name** literally contains the
substring `(2023)` (`Телевизор Samsung 32" серия The Frame QLED Full HD
(2023) LS03C`) — a stale marketing-copy artifact GalaxyStore itself never
updated. Investigated directly against the live page: its structured
`SM_PARAMS.product.specifications` spec **"Год выпуска" (year of
release) explicitly says `2026`**, not 2023. The pipeline already does
the right thing here by design — `year` is read only from that structured
spec field (`normalize.normalize_year`), never parsed out of the name
string or assumed from the catalog URL — so `products.year = 2026` for
this row is a faithful reproduction of what the source site's own
structured data asserts, not a forced/hardcoded value. The name/year
mismatch is the *source's* inconsistency, documented here rather than
silently "corrected" in either direction.

### Non-4K/FHD resolution outliers (investigated, not bugs)

- `UE32H5000FUXRU` (`1366x768`) — genuinely a budget "HD" category TV
  (`category = "HD"`), not a 4K model.
- `MNA114MS1CCXRU` (`4968x2808`) — a 114" **"Дисплей" (professional
  Micro LED display)**, not a consumer TV; the catalog listing under
  `/catalog/televizory/` includes some non-TV Samsung display products.
- `UE27LSM7FAXXPY` (`2560x1440`) — a 27" portable "Дисплей" (The
  Movingstyle), same non-TV-display note as above.

None of these are extraction defects — each value was independently
cross-checked against the live product page's own spec table.

## Known limitations / open questions

- **Automatic deactivation deferred.** No "mark unseen products
  unavailable" step exists yet — see "Persistence" above. A future phase
  should implement it only after a *complete, successful* full-catalog
  crawl can be distinguished from a partial one (`ingestion_runs.status`
  already supports this). Both full-catalog runs above were complete
  successes (`status = success`, 0 failed), so this would have been a
  valid trigger point if the function existed — it deliberately doesn't
  yet.
- **`series` model-code fallback is a heuristic**, not verified against
  Samsung's full official model-code specification — see
  `normalize.normalize_series` docstring. Prefer the catalog's
  `articleMain` hint whenever available (i.e. ingest via `run`, not
  standalone `fetch-product`, when `series` matters).
- **MPN/model-code reliability is now confirmed across the full 75-product
  catalog** (see "Full-catalog MPN/model-code analysis" above): 100%
  non-null, 100% unique, 100% cross-source consistent. Canonical identity
  deliberately remains `(source, external_id)` regardless — see that
  section for why this evidence doesn't by itself justify changing it.
- **Catalog scope includes some non-TV "Дисплей" (display) products**
  (professional Micro LED display, portable display) alongside actual
  TVs — see "Non-4K/FHD resolution outliers" above. Validation doesn't
  exclude them (nothing in the task brief asked it to), so they're
  ingested like any other Samsung product from this source.
- **`resolution` known-label fallback** covers only common labels (8K,
  4K, Full HD, HD Ready/HD); an unrecognized label with no explicit `WxH`
  pattern yields `NULL` rather than a guess.
- No handling yet for products that redirect or 410/discontinue mid-crawl
  beyond the existing `ingestion_errors` recording — not exercised by
  either full-catalog run (0 fetch failures in both).
