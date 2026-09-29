# Phase 4B — Structured Consultant Core

Status: implemented and awaiting review. This phase implements the deterministic part of the
[Phase 4A architecture](PHASE_4A_AI_CONSULTANT_ARCHITECTURE.md): a pre-built structured plan goes through
validation, routing, typed catalog retrieval, feature evaluation, ranking and shortlist composition.
It has no LLM, no embeddings, no network and no writes. Natural-language understanding (4D), vector
execution (4C), answers (4E) and conversation (4F) are not implemented.

## Modules (`consultant/`)

| Module | Responsibility |
|---|---|
| `schemas.py` | Enums (Intent, Route, SortKey, …), `Filters`, `QueryPlanDelta.from_dict` (closed keys and enums at every level), `ResolvedPlan`, `RoutePlan`, row/feature/ranking/shortlist/resolution types |
| `vocabulary.py` | Closed catalog vocabularies (codes, enums, sizes, families) with no live prices or availability; code normalization, family resolution, near-match suggestions |
| `extract.py` | Deterministic hints: full codes (unknown catalog-shaped codes are reported, not dropped), family tokens, sizes, prices (`200к`, `2 млн`, `до 200 тысяч`), Hz, panel/category/resolution keywords, superlatives, list-price and availability wording. Needs such as "для PS5" are left to 4D |
| `catalog_repository.py` | **Only module that builds SQL.** Holds the canonical `EFFECTIVE_PRICE_SQL`, `PRODUCT_KIND_SQL`, allowlisted column/sort/group mappings, parameterized values, hard caps (list 50, candidates 200), tie-aware `extreme()`, `count()`, `get_specs()`, `resolve_model_refs()`, and `open_readonly_connection()` (read-only session + `statement_timeout`) |
| `features.py` | Feature registry v1 (tri-state), dimension parser, use-case profiles v1 |
| `planning.py` | Delta → `ResolvedPlan`: registry/vocabulary validation, availability default, recommendation scope, model/family resolution, family-comparison status, profile gaps |
| `router.py` | Pure `route(plan)`, following the Phase 4A rules R1–R9 |
| `ranking.py` | `rank_candidates()` (fit) and `compose_shortlist()` (exposure), which are separate |
| `retrieval.py` | Executes the four structured routes. Semantic routes return `executed=False` |

`evaluation/catalog_check.py` now imports `EFFECTIVE_PRICE_SQL` from the repository, so there is a
single effective-price rule in the codebase.

## Read-only catalog inventory

Collected with `python -m evaluation.consultant_inventory`. It is SELECT-only, runs in the existing read-only
session (`default_transaction_read_only=on`, verified before any query), and prints no credential.
Evidence is in `evaluation/results/consultant_inventory_4b.json`. A curated 31-product fixture
(`tests/fixtures/consultant_catalog_subset.json`) holds only registry-relevant specs.

- **`series`**: 75/75 populated, 24 distinct values. Clean family codes for S95H, S90H, S85H, QN80H,
  QN70H, M70, M80H, R85H, R95H, M1E, QN1E. The rest are model-code suffixes left by the ingestion
  heuristic (`LS03HAUXPY`/`LS03HEUXPY`/`LS03HWUXPY`, `U8000HUXPY`, `QN90FAUXRU`, `F6000FUXRU`), plus
  `MS1С` with a **Cyrillic С**. Resolution therefore uses `series` first and falls back to a
  model-code stem prefix, with Cyrillic-lookalike normalization.
- **`spec_name`**: 100 distinct names. Registry-relevant coverage (products): Мощность звука 75,
  Технология HDMI ARC 74 (`eARC` 73 / `ARC` 1), Другие технологии оптимизации изображения 70 (VRR
  listed on 34), Режимы просмотра 69 (all mention Filmmaker Mode), Поддержка форматов звука 65
  (`Dolby Atmos` 62 / `Dolby Digital` 3), Стандарт VESA 62 (one `Нет`), Размер без подставки 59,
  Игровая панель 50, Технология FreeSync 39 (Premium 15 / Premium Pro 24), Антибликовое покрытие
  27 (`Да` 24 / `Anti Reflection` 3), ALLM 16, **Версия HDMI 1** (`2.1`, only the professional
  display).
- Presence specs (ALLM, Game Bar, Игровой режим, …) only ever say `Да`. No `Нет` exists for them,
  so absence can only mean `not_listed`.
- No brightness (nits) specification exists. Nothing mentions `VRR`, `ALLM` or `HDMI 2.x` outside
  the specs listed above.
- **Dimension anomalies** (59 rows): Cyrillic `х` separator (`MRE115MR95FXRU`) and a tab
  (`UE43F6000FUXRU`), both tolerated. Malformed components `70.8.8` (`QE55LS03HAUXPY`) and
  `70.5.6` (`QE55S85HAEXPY`). An mm-scale row (`MRE100R85HUXPY`, width 2229.8). Depth `52`
  (`QE65QN80HAUXPY`, while its siblings are 5.2).
- Store product kind from the name's first word: 73 `Телевизор`, 2 `Дисплей` (`MNA114MS1CCXRU`,
  the professional Micro LED display at 19,999,990 ₽ and 120 W; `UE27LSM7FAXXPY` Movingstyle).

## Feature registry v1 (`features-v1`)

The tri-state rule is uniform:
- `yes`: the catalog states the feature.
- `no`: only an explicit negative. That is a typed/numeric comparison, `Нет`, or an explicit value of
  another kind (FreeSync `Premium` for `freesync_premium_pro`, `ARC` for `earc`, HDMI `2.0`).
- `not_listed`: the spec is absent or silent. A multi-value list that omits an item is **not** read as a negative.

Numeric features return a value, or `not_listed` plus a `data_quality` reason. Every result carries
evidence (column or spec name and raw value).

| Feature | Predicate (verified values) |
|---|---|
| `hz_120` | column `refresh_rate_hz >= 120` (<120 → no) |
| `vrr` | `Другие технологии оптимизации изображения` contains `Variable Refresh Rate` |
| `freesync_premium` / `freesync_premium_pro` | `Технология FreeSync` contains `FreeSync Premium` / `Premium Pro` |
| `allm`, `game_bar` | spec value `Да` |
| `hdmi_2_1` | `Версия HDMI` ≥ 2.1 (1/75 listed) |
| `earc` | `Технология HDMI ARC` contains `eARC` (`ARC` → no) |
| `anti_glare` | `Антибликовое покрытие` `Да` / `Anti Reflection` |
| `filmmaker_mode` | `Режимы просмотра` contains Filmmaker / режиссер |
| `dolby_atmos` | `Поддержка форматов звука` contains `Atmos` |
| `sound_power_w` (numeric) | `Мощность звука, Вт` strict number |
| `depth_cm` (numeric) | `Размер без подставки (ШxВxГ), см`: exactly 3 numeric components; width within 0.8–1.3 × expected for the diagonal; depth 0–15 cm; otherwise `not_listed` + `malformed_component` / `unit_scale_mismatch` / `implausible_depth` / `unexpected_component_count` |
| `vesa` | `Стандарт VESA` `N x M` (`Нет` → no) |

Use-case profiles (`use-cases-v1`):

| Use case | Preferred features | Numeric tie-break | Gap |
|---|---|---|---|
| gaming | hz_120, vrr, freesync_premium, allm, game_bar | — | — |
| movies | filmmaker_mode, dolby_atmos | — | — |
| sound | dolby_atmos | sound_power_w ↓ | — |
| bright_room | anti_glare | — | `brightness_not_in_catalog` |
| thin_wall | vesa | depth_cm ↑ | — |
| compact | — | screen size ↑ (soft) | — |

`hdmi_2_1` is deliberately **not** a gaming signal: with 1/75 coverage it would reward listing
completeness rather than capability.

## Policies

- **Availability default**: recommend, list and superlative use available products only, and report
  `excluded_unavailable_count`. Lookup, spec question and compare see everything.
- **Recommendation scope (`recommendation-scope-v1`)**: `recommend` excludes product kind `display`
  (the name starts with `Дисплей`), a structural store signal rather than hard-coded IDs, unless the plan
  names a category, names models, or sets `include_special_products`. The count is reported as
  `excluded_out_of_scope_count`. Factual intents (list, superlative, lookup) never exclude them. For
  example, "most expensive" returns the professional display.
- **Ranking v1**:
  1. The required gate: all `yes` → qualified; any `no` → rejected; otherwise unknown, kept separately.
  2. Preferred-feature `yes` count, descending.
  3. Profile numeric signals.
  4. Effective price, ascending.
  5. `(source, external_id)` as the final stable tie-break.

  No weights. `RankedCandidate.explain()` exposes every signal.
- **Shortlist composition** (max 8, separate from ranking): budget → `top-ranked-v1`; no budget
  with preferences → `price-tiers-v1`; constraint-only → `panel-diversity-v1`. Diversity only chooses
  within the **top fit band** (identical preferred count *and* numeric values), and only when that
  band exceeds 8. Items stay in ranking order, and a lower-fit product can never displace a higher-fit one.
- **Family comparison**: a user size resolves only when every family offers it. Exactly one shared size
  resolves deterministically. Several shared sizes give `ambiguous_multiple_shared_sizes` → `CLARIFY`
  with the options. The largest size is never picked silently. Per-family common typed facts are exposed only when
  uniform.
- **Ordering invariant**: any plan with a sort key (cheapest, most expensive, largest, smallest, highest Hz)
  routes to `SQL_AGGREGATE`/`SQL_FILTER`. This is property-tested across all sort keys, directions,
  intents and limits, with and without a free-text need.

## Evaluation (production, read-only)

`python -m evaluation.run_consultant_structured` scores the unchanged 21 Phase 3D cases with the
unchanged `evaluation.scoring` gates. It uses hand-authored plans (`evaluation/consultant_gold_plans.json`,
written from query text only, with authoring rules recorded in the file). Results are in
`evaluation/results/structured_core_4b.json`.

| Family | Phase 4B | Phase 3D.2 baseline |
|---|---|---|
| sql_sufficient | 7/7 exact | 7/7 |
| aggregate_not_retrieval | 2/2 exact, route `SQL_AGGREGATE` (no vector) | 2/2 |
| hybrid | filter correctness 3/3; Hit@1 0.667, Hit@3/5 1.0, MRR 0.778 | 3/3; Hit@1 0.333, Hit@5 0.667, MRR 0.519 |
| **former vector_primary** (6) | **Hit@1 0.333, Hit@3 0.500, Hit@5 0.667 (4/6), MRR 0.458** | Hit@1 = Hit@3 = Hit@5 0.167 (1/6), MRR 0.210 |
| difficult_ambiguous | VRR: 33 confirmed, 31 not listed, labelled product at rank 5; stale year surfaced (Hit@1); budget-vs-large: `SEMANTIC_FALLBACK`, not run | informational |

Per case (best labelled rank, 4B vs 3D.2): gaming 5 vs 14; sound 1 vs 34; thin-wall 2 vs 53;
kitchen 1 vs 1; **bright-room 44 vs 9 (worse)**; **movies 37 vs 34 (fail)**.

Honest reading of the failures:

- **bright-room**: none of the three Phase 3A labels (`QE65QN80H`, `MRE65R85H`, `UE65M80H`) has any
  anti-glare spec, which is the only bright-room signal the catalog contains (27 products). Labels and
  catalog evidence disagree, and without brightness data neither can be verified. This is a label/data
  question for review, not something to tune.
- **movies**: Filmmaker Mode (69/75) and Dolby Atmos (62/75) are near-universal, so most products tie
  at 2/2 and the price tie-break surfaces budget models. The catalog has no discriminating structured movie
  signal. That is a candidate for Phase 4C semantic evidence or a product decision, not for weights.
- **gaming**: `QE65S95HAUXPY` ranks 21 because ALLM is listed on only 16 products (so S95H scores 4/5)
  and ties are broken by price. The shortlist contains 2 of 3 labelled products.
- Rank metrics are computed over the full ranking, and shortlist membership is reported per case.
  Recommendation scope removed `UE27LSM7FAXXPY` (a "kitchen" label) by design.

## Tests

- Unit (no DB): 413 passed. Of these, 281 are new Consultant tests across schemas, extraction, vocabulary/family, features,
  ranking/shortlist and router.
- Disposable PostgreSQL + pgvector (`tests/run_db_tests.sh`, real migrations, fixture seeded): 59 passed,
  of which 40 are new (repository and end-to-end integration).

## Remaining for Phase 4C

Vector execution for `PRODUCT_SCOPED_SEMANTIC` / `SEMANTIC_FALLBACK`; section passages for shortlisted
products; evidence bundle; review of the bright-room labels and of a movies signal against 4B evidence;
the consultant read-only DB role (owner action) before any non-local run.
