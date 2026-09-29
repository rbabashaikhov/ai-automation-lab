# Phase 3D — Retrieval evaluation (final report)

Status: complete. Evaluation only; nothing here is production architecture. Production data, schema,
chunks, embeddings and the production n8n workflows (Indexing, Retrieval Smoke Test) were never modified.

Catalog under test: 75 products (66 available), 4151 specs, 514 chunks (7 sections), all embedded
(`text-embedding-3-small`, 1536 dims), PostgreSQL 16 + pgvector 0.8.6, exact scan (no ANN index).

## Methodology and artifacts

| Step | What | Where |
|---|---|---|
| 3D.1 | Audit + validate the 21-case dataset against production; deterministic harness (Hit@K, MRR, exact-set, filter correctness, chunk metrics) | `evaluation/{dataset,metrics,scoring,catalog_check}.py`, [AUDIT.md](../evaluation/AUDIT.md) |
| 3D.2 | Real baseline: SQL, vector, hybrid (SQL candidates + unchanged `match_product_chunks`) | `evaluation/run_baseline.py`, `evaluation/results/baseline_3d2.json` |
| 3D.3 | Consultant feasibility spike: top-8 vector candidates + structured facts + chunks -> LLM | `evaluation/consultant_context.py`, `results/consultant_spike_*.json` |
| 3D.4 | Control: every available product as a compact fact sheet -> LLM | `evaluation/fullcatalog_context.py`, `results/fullcatalog_spike_*.json` |

Rules held throughout: read-only DB session; effective price = `COALESCE(sale_price, price)` unless a
query asks for list price; products identified by `model_code` (unique in this catalog) while the
canonical identity remains `(source, external_id)`; no reranking, query rewriting or new embeddings.

Query embeddings (12 texts, 12 OpenAI requests) and the LLM calls (2 x 5 chat calls, `gpt-4.1-mini`,
temperature 0) were made by the operator manually executing a temporary, inactive n8n workflow that
uses the existing n8n-managed OpenAI credential; results were read back through the read-only
executions API. No key ever reached Python or this repository.

## 3D.1 — dataset validation (production, 75 products)
21 cases; derived families: sql_sufficient 7, vector_primary 6, hybrid 3, difficult_ambiguous 3,
aggregate_not_retrieval 2 (aggregate wins over difficult for `difficult-cheapest-superlative`). All 33
model codes resolve to exactly one product. Ground truth corrected after validation:
`structured-budget-under-30k` (+`QE32Q5FAAUXPY`, now 4 products), `hybrid-oled-65-ps5` (all three 65"
OLEDs, since all carry FreeSync/VRR text), `hybrid-neo-qled-budget-gaming` (+`QE75QN80HAUXPY` under
effective price). Details in AUDIT.md.

## 3D.2 — retrieval baseline

| Family | Cases | Result |
|---|---:|---|
| sql_sufficient | 7 | 7/7 exact (precision = recall = 1.0) |
| aggregate_not_retrieval | 2 | 2/2 exact, vector retrieval not used |
| vector_primary | 6 | Hit@1 = Hit@3 = Hit@5 = 1/6 (0.167), MRR 0.210 |
| hybrid | 3 | filter correctness 3/3; Hit@1 0.333, Hit@3 = Hit@5 0.667, MRR 0.519 |
| difficult_ambiguous | 3 | informational: Hit@5 2/3, MRR 0.674 |

Best expected-product rank (of 75 products) for the vector cases: gaming 14, bright room 9, movies 34,
sound 34, thin-wall 53; the only pass is `semantic-portable-kitchen` (rank 1). Observed: the right
*section* usually ranks first (all top-5 `gaming` chunks for the gaming query, all `audio` for sound), but
ranking within it does not track the numbers that matter (a bare 32" "Игровой режим: Да" chunk outranks the
S95H's rich gaming chunk; a 10 W portable outranks the 70 W S95H). `hybrid-oled-65-ps5` cannot discriminate
SQL-only from hybrid (all three candidates are expected answers). Chunks are short labelled spec lists
with the product header repeated, no descriptive prose. `match_product_chunks` cannot express refresh
rate, category, model code, effective price or ordering.

## 3D.3 — consultant spike (top-8 vector candidates)
Five weak queries; candidates = first 8 distinct products by chunk rank, fixed structured-fact whitelist,
best chunks per product. Coverage of a reference set (my read-only SQL definition of good answers, not
used for selection) was poor: gaming 2/39, bright room 1/27, movies 5/61, sound 2/9, thin 1/14.
Answers (27,873 prompt + 1,797 completion tokens): **1 PASS, 3 WEAK, 1 FAIL**. Grounding was mostly good and
numeric reasoning correct (120 > 70 > 20 W, depth 2.55 < 3.41 < 7.2 cm) but the needed products never
reached the LLM. One hallucination: anti-glare claimed for a product with no such spec. No answer said the
evidence was insufficient although the prompt allowed it.

## 3D.4 — full-catalog control (all 66 available products, no truncation)
Compact fact sheet, sorted by model code, absent values shown as `not listed`, raw values preserved
(4 syntactically malformed dimension strings and 2 implausible-scale values reported, not corrected);
16.9-21.5K user tokens per query. All available products present exactly once; S95H, 37/39 gaming, 26/27
anti-glare, 14/14 thin, 9/9 high-sound products now in context. Answers (95,572 + 2,483 tokens):
**0 PASS, 4 WEAK, 1 FAIL**. Bright-room improved FAIL -> WEAK (all anti-glare claims correct). But:
answers still gravitate to 114-115" luxury models while mainstream matches in context go unused (e.g.
28 models with 120 Hz + FreeSync Premium <= 400k RUB in the sheet, none recommended), and new
transcription errors appeared: a non-existent model code `QE55S90HAEXPY` with a neighbouring row's price
(movies), a wrong price 46 990 vs 45 490 (gaming), a channel count copied from another product (sound),
and a claim that no small high-power model exists although a 55" 70 W S95H was in context.
Confound (unavoidable by design): the same top-8 supporting chunks were attached and anchored some picks.

## Architectural conclusion
1. Pure vector similarity is useful for semantic *evidence* retrieval but insufficient as the sole
   product-candidate selector (3D.2; needed products ranked 9-75).
2. Top-8 vector truncation loses many valid products (3D.3 coverage table).
3. Sending the whole available catalog removes the evidence gap but does not improve answer quality and
   adds row/model/spec transcription errors (3D.4). Candidate retrieval was therefore only *partly*
   responsible for the 3D.3 weakness; selection and transcription by the LLM over a large table is the
   other part.
4. Recommended production shape (a decision, not a measured result): a bounded candidate set produced
   primarily from structured catalog constraints, semantic chunks as supporting evidence, LLM reasoning over
   a compact context.
5. Critical output facts (model code exists, price, availability, key specs quoted) should later be
   validated deterministically against PostgreSQL.

## Recommended Phase 4 boundary
In scope: (a) constraint-first bounded candidate selection (roughly 8-15 products) from typed columns and
whitelisted specs, using the effective-price rule; (b) supporting semantic chunks for those products;
(c) the `not listed` convention and a small fixed fact whitelist; (d) a post-answer validator against
PostgreSQL (model code, effective price, availability, quoted specs); (e) start with a measured spike on
the 21 cases and the five spike queries before building.
Open decisions Phase 4 must settle: how constraints are obtained from free text (untested here);
whether to extend `match_product_chunks` or query SQL directly; how to handle queries with no budget
(the model defaulted to luxury products); catalog data quality (malformed dimension strings) belongs to
ingestion, not the consultant. Out of scope until evidence says otherwise: reranking, chunk redesign,
schema or embedding changes.

## Evaluation-only n8n workflows (not production architecture)
`workflows/evaluation-query-embeddings.json` is committed; the two chat-completion workflows
(`evaluation-consultant-spike.json`, `evaluation-fullcatalog-spike.json`) are generated, git-ignored
artifacts that `python -m evaluation.make_llm_workflow [fullcatalog]` rebuilds from the committed contexts.
All were temporary, inactive, manual-trigger workflows used only to run this evaluation with the existing
OpenAI credential, deployed in turn to one remote workflow (`CF5wtjEB9MFR5f0N`, which currently holds the 3D.4
definition). They contain no database node. Delete the remote workflow when the evaluation is no longer needed.
