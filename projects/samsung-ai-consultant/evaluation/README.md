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

## Phase 4E.1 (see [../docs/PHASE_4E_QUERY_SEMANTICS.md](../docs/PHASE_4E_QUERY_SEMANTICS.md))

- `semantic_cases.json` — 30 gold cases (intent / explicit filters / preferences), including the
  false-constraint traps. Written before the parser; `agent_cases.json` is unchanged.
- `semantic_eval.py` — offline evaluation (no DB, LLM or n8n): field-level gold scoring with the
  invented-hard-filter count, the trap table, shadow tool calls checked at the real tool boundary,
  and the grounding check over the recorded 4D.2E Agent arguments ->
  `results/semantic_eval_4e1.json`.

## Phase 4E.2 (see the Gate 4E.2 part of [../docs/PHASE_4E_QUERY_SEMANTICS.md](../docs/PHASE_4E_QUERY_SEMANTICS.md))

- `guard_cases.json` — 22 supplementary semantic-guard cases (explicit requirements, carried
  constraints, thin_wall / compact, model-code size, invented constraints), written before the replay.
- `guard_replay.py` — offline replay of the frozen 4D.2E Agent calls and the supplementary cases through
  the guard, with re-validation at the tool boundary and a no-invention check ->
  `results/guard_replay_4e2.json`. No DB, LLM or n8n.

## Model bake-off gate

- `model_bakeoff.py` — fixes the experiment (models, stages, order, elimination and execution-failure rules) and
  loops the unchanged live tooling over it. The model id is replaced only in the driver's inline copy of the
  Consultant workflow (`agent_live.with_model`), so the deployed workflow never switches model; each trace records
  the model, temperature and API mode n8n actually sent. `manifest` prints the frozen configuration with sha256.
- `bakeoff_cases.json` — 10 supplementary cases / 17 turns (explicit HDMI 2.1, invented-constraint traps, multi-turn
  budget / size / technology / feature retention, budget override and release), built from conversations accepted
  in 4E.2 / 4E.2A and written before the first run. `agent_cases.json` is unchanged.
- Stage 1: 19 of the 42 frozen cases plus the 10 supplementary ones (29 cases / 38 turns, 7 multi-turn), the same
  for every model. Stage 2: the full 42 cases / 44 turns for the models that remain competitive.
- `bakeoff_report.py` — post-processing only: scores the traces with the unchanged `agent_live` / `agent_eval`, joins
  the guard's logged decision per tool call, and writes `results/model_bakeoff.json` (metrics, per-turn verdicts),
  `results/model_bakeoff_conversations.{json,md}` (Conversation Audit Log of every scored dialogue; no model
  reasoning) from `results/model_bakeoff_manual.json` (reviewer rubric and verdicts).
- Result and recommendation: [../docs/MODEL_BAKEOFF.md](../docs/MODEL_BAKEOFF.md).

## Phase 4F.1 — final product acceptance design (see [../docs/PHASE_4F_PRODUCT_ACCEPTANCE.md](../docs/PHASE_4F_PRODUCT_ACCEPTANCE.md))

- `acceptance_scenarios.json` — 15 human-style conversations / 53 user turns for the final acceptance of the frozen
  product, written before any live run. `cases` has the shape the unchanged live tooling reads (`agent_live.build_driver`
  / `extract` / `analyze` with `cases_path`); `scenarios` holds the manual rubric per scenario (constraint state after
  each turn, expected and forbidden behaviour with failure class and severity, grounding expectation, pass criteria);
  `_meta` holds the failure classes, global checks and the known failure modes of earlier phases. No user turn is copied
  from the earlier case files.
- `acceptance.py` — offline validator and coverage report (`python -m evaluation.acceptance`), generator of the
  dataset-derived parts of the design document (`render`; `--check` fails when they are stale), and `manifest` (sha256 of
  the dataset and of the frozen product files for the 4F.2 preflight). No DB, LLM or n8n.

## Phase 4F.2 — live product acceptance run (see [results/phase_4f_2/PHASE_4F_2_PRODUCT_ACCEPTANCE_REPORT.md](results/phase_4f_2/PHASE_4F_2_PRODUCT_ACCEPTANCE_REPORT.md))

- `acceptance_run.py` — evaluation-only loop of the unchanged live tooling (`model_bakeoff.run_case`: temporary inactive
  driver, committed workflow inline, no model override) over `acceptance_scenarios.json`: one session per scenario, each
  scenario once, plus the Agent's memory evidence per turn. A confirmation run is the same command with the failed ids.
- `acceptance_report.py` — `collect` turns the traces and the Consultant's guard log into `evidence_<run>.json`; `render`
  (offline) joins the evidence with `manual_review.json` and `run_manifest.json`, applies the decision rule of the design
  document §4 mechanically, and writes `results.json`, one transcript per scenario and the report; `--check` fails when
  one of them is stale.
- `results/phase_4f_2/` — the run of 2026-10-01: `4F.2 HOLD — PRODUCT ACCEPTANCE FAILED` (4 of 15 scenarios pass;
  3 release blockers). Not changed by Phase 4F.3.

## Phase 4F.3 — MVP demo hardening (see [../docs/PHASE_4F_3_MVP_HARDENING.md](../docs/PHASE_4F_3_MVP_HARDENING.md))

A narrower gate than 4F.1: three defects that would undermine a live demo, re-tested on four 4F.2 scenarios and shown
on eight demo conversations. It does not replace the 4F.2 result.

- `mvp_demo_scenarios.json` — the two suites: `targeted` (PA-02, PA-04, PA-08, PA-15, turns taken unchanged from
  `acceptance_scenarios.json`, each with the defect that must be gone) and `demo` (DEMO-01 … DEMO-08, 22 turns, none
  copied from the acceptance set), with the constraint state and machine-readable limits per turn.
- `mvp_demo.py` — `run <suite> …` (the unchanged live tooling: temporary inactive driver, committed workflow inline,
  one fresh session per conversation); `collect` (traces + the Consultant's guard log → `evidence_<suite>.json`, with
  supporting automated checks: unsupported feature claims, invented numeric arguments, a removed limit still stated in
  the answer, count claims, one value stated for a group, products outside the limits, per-product prices); `render`
  (offline: evidence + `manual_review.json` + `measurements.json` + `run_manifest.json` → `results.json`, transcripts,
  the report, the mechanical gate decision); `--check` fails when a generated file is stale.
- `results/phase_4f_3_demo/` — the runs of 2026-10-01 on image `4f3f`: **`4F.3 MVP DEMO HOLD — FURTHER HARDENING
  REQUIRED`**. The three targeted defects are gone and 7 of 8 demo conversations pass; the hold is one criterion:
  a numeric limit the user never stated is removed before the Core but still worded in the answer.
  `measurements.json` holds 306 repeated sessions (rates per build and question), `evidence_targeted_run1–3.json` the
  targeted runs on superseded builds.
