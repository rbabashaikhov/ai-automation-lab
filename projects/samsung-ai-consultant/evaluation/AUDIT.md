# Phase 3D.1 — audit of `retrieval_cases.json`

21 cases, JSON, authored in Phase 3A (`c069ea2`), corrected in Phase 3D.1 after
validation against the production catalog (75 products). Re-verify with the
read-only `python -m evaluation.catalog_check` (0 mismatches at time of writing;
it checks model-code existence/uniqueness, exact SQL/aggregate sets, that
hybrid/difficult filters admit every expected product, and availability).

## 1. Confirmed catalog facts (production, verified)

- 75 products; 75 distinct non-null `model_code`s and 75 distinct
  `(source, external_id)` identities. All 33 model codes used by the dataset
  resolve to exactly one product (none missing, none ambiguous).
- `QE75LS03HWUXPY`: category The Frame, panel technology Neo QLED (typed column and
  spec "Технология экрана"), 75", 120 Hz. Valid for `structured-neo-qled-75-120hz`.
- All three 65" OLEDs (S85H, S90H, S95H) carry FreeSync/Variable Refresh Rate spec text.
- 12 Neo QLED products have effective price ≤ 200000; exactly three are 120 Hz
  (QN80H 55/65/75"). `QE75QN80HAUXPY` is 229990 list / 189990 effective.
- 4 available products are ≤ 30000 effective (list gives the same set).
- Cheapest by effective price: `UE32H5000FUXRU` 22990 (next: `UE32F6000FUXRU` 23590 on sale).
- Largest OLED: three-way tie at 83" (S85H, S90H, S95H). 9 products unavailable.
- 11 products have no gaming-section specs; 34 mention "Variable Refresh Rate"
  (all in spec group `Изображение`, i.e. the `display` section); S95H sound power
  70 W, QN80H 30 W; S95H depth 2.64 cm; `QE32LS03CBUXRU` is the only Frame whose
  name year (2023) differs from `year` (2026).

## 2. Corrected ground truth (Phase 3D.1)

| case | before | after | why |
|---|---|---|---|
| structured-budget-under-30k | 3 products | 4: + `QE32Q5FAAUXPY` (27990, available) | set was incomplete; note also wrongly said no product had a sale price |
| hybrid-oled-65-ps5 | `QE65S95HAUXPY` only | S95H, S85H, S90H | S95H-only rested on the claim that the other two lack FreeSync/VRR text; the catalog shows all three have it. No distinction is supported |
| hybrid-neo-qled-budget-gaming | QE55QN80H, QE65QN80H | + `QE75QN80HAUXPY` | list price had been used; under effective price it is 189990 ≤ 200000. Note corrected (12 admitted, not 7) |
| difficult-budget-vs-large-tradeoff | unchanged ids | note only | removed the false "3–10x more" claim; stays informational, ids are examples |

Also added `_meta.price_semantics`. Unchanged by decision: `QE75LS03HWUXPY` stays
in `structured-neo-qled-75-120hz`; `expected_sections` are **not** added yet (to be
proposed after observing real chunk retrieval); the derived family of
`difficult-cheapest-superlative` is `aggregate_not_retrieval`.

**Price rule.** Budget constraints use `effective_price = COALESCE(sale_price, price)`
unless the query explicitly asks for list/base/original price. The harness's
`catalog_check` already compiles `max_effective_price` this way.

**Families** (derived: aggregate mechanism > `difficult_ambiguous` intent > mechanism;
`sql_plus_vector` → `hybrid`): sql_sufficient 7, vector_primary 6, hybrid 3,
difficult_ambiguous 3, aggregate_not_retrieval 2.

## 3. Ambiguous / non-deterministic expectations

Scored by "any acceptable product" Hit@K/MRR, never by exact set; never precision/recall.

- Semantic cases (8–12, 20): `relevant_product_ids` are examples, not exhaustive
  (e.g. many products have gaming specs; 3 are listed). `semantic-bright-room` and
  `semantic-movies` intents are fuzzy; `semantic-thin-wall-mount` depends on a
  number inside a compound dimension string.
- `difficult-vrr-buried-in-spec-value`: 34 products qualify, 3 listed; a
  keyword/`ILIKE` over spec text is a legitimate non-vector solution.
- `difficult-budget-vs-large-tradeoff`: no thresholds, no objective rule — informational only.
- `difficult-stale-year-in-name`: expected product is deterministic, but whether to
  trust `year` or the user's "2023" is policy; measure only that it is surfaced.
- `hybrid-oled-65-ps5`: after correction the filter alone yields the full expected
  set, so it cannot discriminate semantic re-ranking. `hybrid-large-120hz-good-sound`
  assumes "большой" = ≥ 75" (22 products admitted, 2 listed).
- `semantic-portable-kitchen` is effectively hybrid (`max_screen_size_inches: 32` admits 5
  products, 3 listed as examples).
- Snapshot drift: availability/price cases depend on live data; re-run `catalog_check`
  before each evaluation campaign.

## 4. Future retrieval limitations (not addressed here)

- `match_product_chunks` filters only year, min/max size, panel technology,
  min/max **list** price and availability. Dataset filters also need `model_code`,
  `refresh_rate_hz`, `category`, effective price and `order_by/limit`; exact size
  needs min = max. Under the effective-price rule, price filtering through the
  function as written is wrong for products on sale.
- Aggregate cases (superlatives) must be solved by SQL; a vector/hybrid mechanism on
  them is scored as a failure.
- Scoring identifies products by `model_code` (unique in the current catalog, but not
  the canonical `(source, external_id)` identity).
- No `expected_sections` yet, so section-rank metrics are unavailable until they are added.

## Per-case summary

| # | id | family | source of truth | expected |
|---|----|--------|-----------------|----------|
| 1–3 | exact-model-code, …-with-brand, exact-lookup-conversational | sql_sufficient | `model_code` | 1 product each |
| 4 | structured-oled-65 | sql_sufficient | panel + size | 3 |
| 5 | structured-neo-qled-75-120hz | sql_sufficient | panel + size + Hz | 2 |
| 6 | structured-budget-under-30k | sql_sufficient | effective price | 4 |
| 7 | structured-largest-oled | aggregate_not_retrieval | MAX(size), ties | 3 |
| 8–12 | semantic-gaming-console, bright-room, movies, sound-without-soundbar, thin-wall-mount | vector_primary | spec text | examples |
| 13 | hybrid-oled-65-ps5 | hybrid | filter + gaming text | 3 (filter set) |
| 14 | hybrid-neo-qled-budget-gaming | hybrid | filter + gaming text | 3 QN80H |
| 15 | hybrid-large-120hz-good-sound | hybrid | filter + audio text | examples |
| 16 | difficult-cheapest-superlative | aggregate_not_retrieval | MIN(effective price) | 1 |
| 17 | difficult-vrr-buried-in-spec-value | difficult_ambiguous | spec text | examples |
| 18 | difficult-budget-vs-large-tradeoff | difficult_ambiguous | none | examples, informational |
| 19 | difficult-stale-year-in-name | difficult_ambiguous | category + year | 1 |
| 20 | semantic-portable-kitchen | vector_primary | size + text | examples |
| 21 | structured-unavailable-models | sql_sufficient | `is_available` | 9 |
