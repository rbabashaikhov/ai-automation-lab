# Phase 4C — Evidence & Semantic Retrieval

Status: implemented and awaiting review. A pre-built `ResolvedPlan` now becomes a complete,
bounded `EvidenceBundle`, ready for a future answer model. The pipeline is Phase 4B structured
retrieval, then 4C passages, gaps, relaxation, confidence and budget. No generative LLM, no new
embeddings (only the Phase 3D cache), no production writes. Phase 4B behaviour and the Phase 3D labels
and 4B gold plans are unchanged.

**Answer to the phase question.** Semantic retrieval adds **evidence**, not **selection**:

- It reliably finds the right section/passage for needs that name a spec concept (sound, gaming, VRR,
  wall/thin).
- It cannot help bright-room or movies, because the corpus holds no discriminating evidence for them.
- As a ranking tie-break it is slightly **harmful** on the Phase 3D cases, so it is off by default.

## Implementation

| Module | Change |
|---|---|
| `catalog_repository.py` | Additive only. `chunks_by_section` (no embedding); `vector_in_products` (candidate/product-scoped; ids from structured retrieval, allowlisted sections, parameterized vector, no chunk price/availability filters); `vector_global` (unchanged `match_product_chunks`, availability filter only, never its list-price filters); `nearest`, `spec_coverage`, `specs_mentioning` |
| `schemas.py` | `ChunkRow` |
| `semantic.py` | `CachedQueryEmbeddings` (the **only** embedder; a miss raises `EmbeddingUnavailable`, and there is no network code); `semantic_tiebreak` (reorders only inside identical structured fit); `probe_terms`, `mentions`, `lexical_probe` (absence decisions) |
| `relaxation.py` | One-constraint-at-a-time SQL probes |
| `evidence.py` | Evidence contract, builder, sanitization, fact ids, confidence, budget, LLM serialization |
| `render.py` | Deterministic debug renderer |
| `evaluation/run_consultant_evidence.py` | Read-only Phase 4C evaluation → `results/evidence_semantic_4c.json` |

### Semantic paths

| Path | When | Embeddings |
|---|---|---|
| Section lookup | Mapped features/use cases (section from the registry: gaming→gaming+display, sound→audio, movies→display+audio, thin_wall→physical_design, spec questions → attribute section, lookup → sanitized overview) | **0** |
| Candidate-scoped vector | `CONSTRAINT_FIRST` + `free_text_need`; ids = structured candidates; non-overview sections | 1 cached lookup |
| Product-scoped vector | `PRODUCT_SCOPED_SEMANTIC` (resolved products only); top 3 passages | 1 cached lookup |
| Global fallback | `SEMANTIC_FALLBACK` only; 15-product window, then scope/validation, ≤ 8 shown; confidence `weak` | 1 cached lookup |

A missing cached vector produces a `semantic_unavailable` gap and a fallback to section lookup.
It never causes an API call. Vector hits outside the candidate ids raise (and are counted); the observed count is 0.

### Evidence contract

`EvidenceBundle` contains:
- `products` (P1…Pn, in shortlist/result order) and `alternatives` (A1…An, from relaxation);
- `gaps`, `totals`, `retrieval_confidence` + reasons, `semantic` diagnostics, `budget`, `plan_summary`.

`ProductEvidence` keeps the handle, internal id, canonical `(source, external_id)`, model code, name,
URL, live typed facts, relevant spec facts, `FeatureStatus` (tri-state + fact ids + data quality),
`ConstraintStatus` (live value vs each user constraint), passages, LLM-safe selection reasons, and a
separate `ranking_debug` (structured/final rank, fit) that is never serialized for the LLM.

**Fact ids.** `P1.col.<column>`, `P1.spec.<spec_key>` (unique per product by
`UNIQUE(product_id, spec_key)`), and `P1.feat.<feature_id>`. They are deterministic and validated to
`[a-z0-9_]`; any other component raises.

**Live facts vs snapshots.** Price and availability come only from the request's `ProductRow`.
Every passage is sanitized: `Цена:` / `Наличие:` lines are removed and counted. The tests change a
price after indexing and check that the evidence carries the new price, while the stale chunk price
appears nowhere in the LLM payload.

**LLM serialization** (`serialize_for_llm`) is compact deterministic JSON. It contains handles, model
code, name, facts with ids, feature states, constraint status, passage section + text, LLM-safe
selection reasons, gaps and non-zero totals. **Excluded:** similarity, fit/ranks, internal ids,
`(source, external_id)`, URLs, raw payload, description, and stale price/availability lines.

### Gaps

- Normalized kinds: `model_not_found`, `attribute_not_in_catalog`, `attribute_not_listed_for_product`,
  `attribute_absence_unverified`, `no_product_satisfies`, `required_feature_not_listed`, `data_quality`,
  `excluded_unavailable`, `excluded_out_of_scope`, `not_in_catalog_domain`, `semantic_unavailable`,
  `year_mismatch`, `family_size_ambiguous`, and the 4B informational kinds.
- 4B gap kinds are mapped, not re-derived. For example, the bright-room `brightness_not_in_catalog`
  becomes `not_in_catalog_domain`.
- Data-quality gaps come from 4B feature results of shown products.
- Gaps are never trimmed.

### Absence rule (Phase 4A amendment)

A vector miss is never evidence. For long-tail questions, `lexical_probe` inspects the product's
real spec rows with one boundary-aware matcher (`'2.1'` does not match `22.1`). The **same matcher**
computes catalog-wide coverage, so the two can never disagree.

- **Mentioned in specs:** the matching rows become facts. `all_terms_matched` says whether a mention
  of "HDMI" alone also confirms "2.1".
- **`attribute_not_listed_for_product`:** only when no row matches and *every* term is used
  elsewhere in the catalog.
- **`attribute_absence_unverified`:** otherwise. A deliberate example is "голосовой помощник" vs the
  catalog's `Голос`: the stem doesn't match, so the result is uncertainty, not a false "not listed".
- For registry attributes, a spec name with 0 catalog coverage gives `attribute_not_in_catalog`.

### Relaxation

When there are zero candidates, or no product confirms a required feature, each user constraint is
dropped in turn, with every other constraint and policy kept. Alternatives come from SQL only:
- nearest by `abs(value − target)` for exact values;
- ascending/descending for dropped upper/lower bounds;
- price order for set constraints.

Required-not-listed candidates are added as alternatives. Each alternative records `violates:<key>`
and a `ConstraintStatus` with `satisfied=False`. There is at most one probe per constraint and 3
alternatives per probe.

### Confidence (structural, never a cosine threshold)

- `weak`: global fallback, no products, or the top structured-fit band is larger than the shortlist
  (the shown products are not discriminated by evidence).
- `partial`: a requested feature is not listed, absence is unverified, there is a domain gap, semantic
  evidence is unavailable, or a free-text need is backed by vector passages only.
- `strong`: otherwise.
- `not_applicable`: clarify / no-retrieval routes.

### Budget

- Limits: 8 products (lists 20, named 4), ≤ 2 passages per product (3 for product-scoped), and 6,000
  tokens measured with the existing `indexing.metadata.approx_token_count` (tiktoken `cl100k_base`,
  an approximation of the future answer model's tokenizer).
- When over budget, passages are trimmed from the lowest-ranked product first. Products, facts and
  gaps are kept.

## Evaluation (production, read-only session verified; `results/evidence_semantic_4c.json`)

Cached vector lookups: 32 hits, 10 misses. The misses are the 10 long-tail probe needs, which have no
cached vector, and are reported as `semantic_unavailable`. OpenAI requests: 0; embeddings generated: 0.

### A. Phase 4B baseline (re-run, identical to the committed result)

| | Hit@1 | Hit@3 | Hit@5 | MRR |
|---|---|---|---|---|
| Former vector-primary (6) | 0.333 | 0.500 | 0.667 | 0.458 |
| Hybrid (3) | 0.667 | 1.000 | 1.000 | 0.778 |

SQL 7/7, aggregate 2/2, hybrid filter correctness 3/3, as in 4B.

### B. Evidence coverage (21 unchanged gold plans)

- Every SQL/aggregate/lookup label is represented (1/1 … 9/9). Recommend cases: gaming 2/3, bright-room
  **0/3**, movies 1/3, sound 2/2, thin-wall 3/3, kitchen 2/3 (Movingstyle out of scope by design),
  hybrids 3/3, 3/3, 2/2, VRR 1/3.
- No plan feature is missing from the evidence. **Embedding lookups: 0** for the 20 mapped plans and 1
  (cached) for the unmapped global-fallback plan.
- Size (approx. tokens): lookups 408–564; lists 909–3,085; recommendations 2,677–5,928 (gaming and the
  Neo QLED case trimmed 13 passages each, movies 6, thin-wall 3); fallback 5,879. **All ≤ 6,000.**
- Stale lines stripped: 2 per lookup overview, 12 in the fallback (overview chunks in vector hits).
- Confidence: strong for all SQL cases and most recommendations; `partial` for VRR
  (required_feature_not_listed); `weak` for bright-room and movies (top fit band > shortlist) and the fallback.
- Similarity is present in the LLM payload in 0 of 21 cases.

### C. Candidate-scoped semantic (experimental arm: the case query as `free_text_need`)

Gold plans are not edited. This arm adds the query text as a need so the vector path can be
measured. The passage gold signal is the registry section of the plan's features.

- Escape violations: **0** (64/3/9/19 candidates).
- The first chunk ranked in a gold section: rank 1 for every case except bright-room (rank **78**).
- Top-passage section correctness of shown products: **1.0** except bright-room (**0.0**).

Ranking impact of the fit-tie-only semantic tie-break:

| | Hit@1 | Hit@3 | Hit@5 | MRR |
|---|---|---|---|---|
| Vector-primary: 4B structured → 4C final | 0.333 → **0.167** | 0.500 → 0.500 | 0.667 → 0.667 | 0.458 → **0.385** |
| Hybrid: 4B → 4C | 0.667 → 0.667 | 1.000 → **0.667** | 1.000 → 1.000 | 0.778 → **0.750** |

Per label (structured → final):
- gaming: S95H 21→33, QN80H 8→21, R85H 5→4
- sound: 2→3, 1→2
- VRR: 21→28, 15→20, 5→1
- OLED-65: S95H 3→1
- 120 Hz sound: 3→5

The tie-break never lifts a label into the top 5 that was not there already and costs top-1 hits,
so **it is off by default**. `apply_semantic_tiebreak=True` exists for measurement only.

### D. Product-scoped section location (each product's own chunks, cached queries)

| Need (cached) | Gold section | Section at rank 1 | Signal line at rank 1 |
|---|---|---|---|
| хороший звук без саундбара | audio | 75/75 (1.00) | Мощность звука 1.00 |
| для игровой приставки | gaming | 64/64 (1.00) | — |
| тонкий на стену | physical_design | 0.89 (top-3 0.96) | Размер без подставки 0.97 |
| поддержка VRR | display | 0.88 (top-3 1.00) | Variable Refresh Rate 0.97 |
| для светлой комнаты | display | **0.00** (mean rank 4.6) | Антибликовое покрытие **0.00** |

### E. Product-scoped long-tail probes (truth by independent hand-written patterns)

Across 10 cases: verified present 4 (AirPlay on MR95F, HDMI 2.1 on MNA114, USB-C on Movingstyle,
Wi-Fi 6E on R85H 100"); verified absent 2 (AirPlay and USB-C on S95H 65"); absence unverified 2
(Bixby, never in the catalog; "голосовой помощник" on S95H, whose truth *is* listed as `Голос`);
partial mentions 2 (HDMI 2.1 on S95H surfaces `HDMI: 4 шт`; Wi-Fi 6E on QN70H surfaces `Wi-Fi 5`).
**False absence 0, false full presence 0.** No long-tail need had a cached vector, so product-scoped
passages for these needs were measured only in the disposable-DB tests (synthetic vectors).

## Bright-room and movies decision

| Case | Structured evidence | Semantic evidence | Result | Interpretation |
|---|---|---|---|---|
| bright_room | Only signal: `Антибликовое покрытие` (27 products; **0/3 labels**). No brightness in the corpus (`ярк`, `brightness`, `nits`, `кд/м`: 0 chunks). Labels share only backlight/dimming technology names that are widespread (`Supreme UHD Dimming` 35 products incl. 3/3 labels; `Mini LED` 31, 2/3) | Similarity is flat (display section: top-1 0.209 vs 10th 0.203). Labels rank 7–42 among 64 candidates; the gold section is never the top chunk (rank 78); product-scoped section rank 1: 0% | 4B rank 44; tie-break 32–51 | **Semantic retrieval does not help; corpus evidence conflicts with the labels; corpus evidence insufficient.** Anti-glare is on other products (including the S95H OLED the 3A note called a worse match). Flag the case for label review |
| movies | Filmmaker 69/75, Atmos 62/75, HDR10+ 75/75, Q-Symphony 71/75. `Кино`/Movie Mode on 1 product (`QE32LS03CBUXRU`); Dolby Vision and IMAX: 0. Factual technology names co-occur with the labels (`Perceptional Color Mapping` 15 products, 3/3 labels; 360 Audio 36, 3/3; 70 W 8, 2/3), but nothing in the corpus ties them to movies | Top results are The Frame models (driven by the single `Кино` product); labels rank 29–57; section finding works (display/audio chunk at rank 1) | 4B rank 37; tie-break 32–55 | **Semantic retrieval does not help; corpus evidence insufficient** for a movie-specific signal. Turning technology names into a movie signal would be a product/domain decision (a new registry feature), not a retrieval fix |

## Tests

- Unit: 431 passed (18 new Phase 4C). Disposable PostgreSQL + pgvector: 86 passed (27 new). The new
  DB tests use real chunk text from the indexing builder and deterministic synthetic vectors, with no OpenAI.
- They cover: restriction/ordering/NULL exclusion/section allowlist; read-only session; zero embeddings
  for gaming/sound/movies/connectivity/physical design; candidate escape; opt-in tie-break; missing
  vector → gap; product-scoped present/absent/unverified and "semantic miss is not absence"; live
  price over stale chunk; LLM payload hygiene; gaps preserved under budget; data quality; relaxation
  (budget, size, required feature); global fallback scope; renderer.

## Limitations

- Only 12 cached need vectors exist. There is no live product-scoped vector measurement for real
  long-tail needs; Phase 4D will embed need text at runtime and needs the credential decision (4A D1).
- The lexical probe is Russian-stem heuristics without morphology, so it errs toward "unverified".
- Token counts are cl100k approximations.
- Structural confidence marks most no-budget recommendations over a wide tie as `weak`, which is
  intentional. The `weak` reasons list does not also enumerate the partial gaps; those are in `gaps`.

## Recommendation for Phase 4D

Proceed to query understanding with semantic retrieval scoped as measured:
- section lookup for mapped needs;
- vector passages only for unmapped needs;
- no semantic ranking.

Before 4E, decide: bright-room label review; whether a movie signal is a product decision; and
whether the answer layer should surface `weak` confidence as a clarifying question (budget/size) rather
than a recommendation.
