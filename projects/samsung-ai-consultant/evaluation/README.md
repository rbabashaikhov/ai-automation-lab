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
