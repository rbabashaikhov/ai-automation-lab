# Retrieval evaluation harness (Phase 3D.1)

Foundation only: it loads and validates `retrieval_cases.json`, scores retrieval
results **you supply**, and summarizes. It does not run retrieval, call a
database (except the optional read-only `catalog_check`) or call OpenAI.
Audit of the dataset and open questions: [AUDIT.md](AUDIT.md).

```python
from evaluation.dataset import load_dataset, select_cases
from evaluation.scoring import CaseRun, ChunkHit, score_run, summarize, render_summary

cases = select_cases(load_dataset(), families=["vector_primary"])
runs = [CaseRun("semantic-movies", "vector", chunks=[ChunkHit("QE65S95HAUXPY", "display", 0.71)])]
results = score_run(cases, runs)          # cases without a run -> status "not_run"
print("\n".join(r.render() for r in results)); print(render_summary(summarize(results)))
```

## Families and what is measured

`family` is derived from the stored `intent` + `expected_mechanism`
(`derive_family`; aggregate wins, then difficult, then mechanism).

| family | n | gate (status) | reported |
|---|---|---|---|
| sql_sufficient | 7 | exact set equality with expected products | precision, recall |
| aggregate_not_retrieval | 2 | exact set (ties: all) **and** no vector/hybrid mechanism used | precision, recall |
| vector_primary | 6 | an acceptable product in top 5 | Hit@1/3/5, MRR, chunk rank, section rank, best similarity |
| hybrid | 3 | filter correctness (if admitted set supplied) **and** acceptable product in top 5 | as vector + filter check |
| difficult_ambiguous | 3 | none — status `informational` | Hit@1/3/5, MRR, chunk metrics |

Definitions: ranks are 1-based over the de-duplicated product list (a product
appearing in several chunks counts at its first chunk); any one acceptable
product satisfies Hit@K/MRR; `filter_correctness` requires returned ⊆ admitted
and expected ⊆ admitted; chunk metrics need `ChunkHit` lists, section rank needs
the optional `expected_sections` case field. Summary macro-averages rank metrics
over cases that have them and excludes `informational`/`not_run` from pass rates.
Products are `model_code` strings (see AUDIT finding 3).

## Price semantics

Budget constraints use `effective_price = COALESCE(sale_price, price)` unless the query
explicitly asks for list/base/original price (`max_effective_price` in dataset filters).

## Catalog validation (read-only, needs DB tunnel)

    python -m evaluation.catalog_check

Tests: `python -m pytest tests/test_evaluation_*.py`.

## Phase 3D scripts (see [../docs/phase-3d-retrieval-evaluation.md](../docs/phase-3d-retrieval-evaluation.md))

- `run_baseline.py` — 3D.2 baseline (SQL / vector / hybrid), writes `results/baseline_3d2.json`.
- `consultant_context.py`, `fullcatalog_context.py` — build (never send) the 3D.3 / 3D.4 LLM contexts.
- `make_embedding_workflow.py`, `make_llm_workflow.py` — generate the temporary evaluation-only n8n workflows.
- `results/query_embeddings.json` is a derived, git-ignored cache; everything else in `results/` is committed evidence.

## Phase 4B scripts (see [../docs/PHASE_4B_STRUCTURED_CORE.md](../docs/PHASE_4B_STRUCTURED_CORE.md))

- `consultant_inventory.py` — read-only catalog inventory for the feature registry
  (`results/consultant_inventory_4b.json`; `--fixture` refreshes `tests/fixtures/consultant_catalog_subset.json`).
- `consultant_gold_plans.json` — hand-authored structured plans for the 21 unchanged cases.
- `run_consultant_structured.py` — gold plan → router → structured retrieval → ranking, scored with the
  unchanged `scoring` gates next to the 3D.2 baseline (`results/structured_core_4b.json`).

## Phase 4C script (see [../docs/PHASE_4C_EVIDENCE_SEMANTIC.md](../docs/PHASE_4C_EVIDENCE_SEMANTIC.md))

- `run_consultant_evidence.py` — read-only: 4B baseline re-check, evidence coverage for the 21 gold
  plans, candidate-/product-scoped semantic measurements with the cached Phase 3D query vectors only,
  long-tail probe cases, bright-room / movies corpus analysis (`results/evidence_semantic_4c.json`).

## Phase 4D (see [../docs/PHASE_4D_AGENT_RUNTIME.md](../docs/PHASE_4D_AGENT_RUNTIME.md))

- `agent_cases.json` — 42-case / 44-turn Agent evaluation set (tool selection, arguments,
  clarification, grounding; adversarial and follow-up families). New file; the Phase 3D dataset and
  4B gold plans are unchanged.
- `agent_eval.py` — dataset validation (every expected argument set must pass the Python tool
  boundary), transcript scoring, deterministic grounding flags (since 4D.2B-R also: catalog claims
  without tool evidence, group claims, availability mismatches, invented arguments), and the n8n
  output adapter (verified on real executions in 4D.2B).
- `results/agent_smoke_4d2b.json` — Gate 4D.2B live smoke and the 4D.2B-R reruns plus policy
  probes (compact transcripts, scores, held-out probe registration, runtime cap test).
- `agent_live.py` — Gate 4D.2C live-run tooling: temporary driver per case (all turns in one session),
  sub-execution extraction via the n8n public API, and measurement layers over the unchanged scorer.
- `results/agent_eval_4d2c.json` — Gate 4D.2C full evaluation: 42 cases / 44 turns, automated and
  manual verdicts per turn, metrics, latency and token estimates.
- `results/agent_regression_4d2d.json` — Gate 4D.2D focused regression after remediation: 14 cases /
  16 turns registered before the run (sha256 in the file), automated and manual verdicts next to the
  4D.2C manual verdict per turn. Since 4D.2D `agent_eval` also flags mislabelled (swapped) prices and
  picture/brightness comparatives, and reads both result contracts (`agent-result-v1` / `-v2`).
- `results/agent_eval_4d2e.json` — Gate 4D.2E final 42-case / 44-turn confirmation on the frozen 4D.2D
  runtime: automated and manual verdicts plus the 4D.2C manual verdict per turn, the price audit,
  infrastructure retries (none semantic), and the preflight record.
- `run_agent_offline.py` — Gate 4D.1: executes the expected tool calls through the real facade in
  the read-only session -> `results/agent_offline_4d1.json`. No LLM.
- `agent_injection.py` — evaluation-only test double adding instruction-like text to tool evidence.
