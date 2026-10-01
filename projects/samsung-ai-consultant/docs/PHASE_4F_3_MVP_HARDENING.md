# Phase 4F.3 — MVP Demo Hardening

Status: **root-cause analysis complete (§3–§5); fixes, tests and live runs are recorded in §8 onwards as
they are done.**

Phase 4F.2 ended with `4F.2 HOLD — PRODUCT ACCEPTANCE FAILED`. That result stands and is not changed by this
phase (§2). Phase 4F.3 has a narrower target, set by the project owner: a **demo-ready MVP for portfolio and
client demonstration**. It removes three defects that would undermine trust in a live demo and leaves every
other 4F.2 finding as a known limitation.

| Artifact | Contents |
|---|---|
| This document | scope, root causes, fix design, regression risks, test and deployment plan, MVP gate |
| [PHASE_4F_PRODUCT_ACCEPTANCE.md](PHASE_4F_PRODUCT_ACCEPTANCE.md) | the frozen 4F.1 rubric and the 4F.2 result (unchanged) |
| [`evaluation/results/phase_4f_2/`](../evaluation/results/phase_4f_2/PHASE_4F_2_PRODUCT_ACCEPTANCE_REPORT.md) | the 4F.2 evidence this analysis is built on (unchanged) |

---

## 1. Scope

**Mandatory targets (the only ones):**

| Id | Target | 4F.2 occurrences |
|---|---|---|
| MVP-1 | No product attribute stated as fact without evidence available to the current turn | PA-02 turn 1 (both runs), PA-04 turn 1: «с поддержкой HDMI 2.1» |
| MVP-2 | A relative or vague request never becomes an invented numeric constraint | PA-08 turn 3: «подешевле» → `max_price 40000` reached the Core |
| MVP-3 | An exact aggregate is answered from exact evidence for that aggregate | PA-15 turn 3: 66 available products presented as «66 моделей с Dolby Atmos» |

**Not in scope.** 15/15 acceptance; the MINOR issues; qualitative recommendation language (G7: «отличное
качество изображения», «лучше для кино»); bright-room and best-for-movies reasoning; ranking, reranking,
chunks, embeddings, catalog sources, the model, the PostgreSQL schema, the n8n architecture, a new agent
framework. The 4F.2 classes outside the three targets stay as known limitations (§9).

**What must not regress** (it held in all 90 turns of 4F.2): no invented model code, price or availability;
no product that breaks an active hard constraint; multi-turn constraint memory; replaced and released
constraints; `not_listed` never reported as «нет».

## 2. Relation to Phase 4F.2

| Statement | Status |
|---|---|
| `4F.2 HOLD — PRODUCT ACCEPTANCE FAILED` | stands; the 4F.2 files are not edited |
| The product is accepted as production-ready under the 4F.1 rule | **no** — not claimed by this phase |
| The product is demo-ready as a portfolio MVP | decided by the MVP gate of §7, after the live runs |

Both statements can be true together. Phase 4F.3 changes the product, so the "frozen product" hashes of the 4F.1
design (§1.2 there) describe the state that was *run* in 4F.2, not the current tree. Three existing tests compared
those records with the working tree; they are re-anchored to the recorded run manifest (§6.3).

---

## 3. MVP-1 — unsupported factual claims

### 3.1 Observed failure

| Run | User | Agent's proposed call | Guard | Answer |
|---|---|---|---|---|
| PA-02 turn 1, original | «Посоветуйте телевизор под PS5, сын в основном в шутеры играет.» | `recommend_tvs {use_cases [gaming], required_features [hdmi_2_1, hz_120], preferred_features [allm, vrr]}` | removed both required features | «…следующие телевизоры с поддержкой HDMI 2.1, 120 Гц и VRR» |
| PA-02 turn 1, confirmation | same | `{use_cases [gaming], required_features [hdmi_2_1], min_refresh_rate_hz 120}` | removed both | «…с поддержкой HDMI 2.1 и частотой обновления 120 Гц» |
| PA-04 turn 1, original | «Хочу OLED на 65, до 300 тысяч. В основном PS5…» | `{…, required_features [hdmi_2_1], preferred_features [hz_120]}` | removed `hdmi_2_1` | «…два OLED телевизора … с поддержкой HDMI 2.1 для PS5» |

The catalog lists an HDMI version for 1 of 75 products (a professional display). For every recommended TV
`hdmi_2_1` is `not_listed`.

### 3.2 Causal path

1. **Where the interpretation originates.** The model's own knowledge: «PS5 → HDMI 2.1». It writes that belief
   into the tool call as `required_features [hdmi_2_1]` (historical failure mode H01). In all three failing
   turns the claimed feature is one the Agent had itself proposed in the same turn.
2. **What evidence exists at that point.** The guard removes the requirement (`semantic_guard._rules`), the Core
   ranks for gaming only, and `agent_payload.to_agent_payload` returns per product the states of the features
   the *plan* contains: `allm, vrr, hz_120, freesync_premium, game_bar`. `hdmi_2_1` is not in the plan any
   more, so **it is absent from every product's `features`**. The only trace is
   `request.not_applied = {constraints: [{argument: required_features, value: hdmi_2_1}], note: "Not stated by
   the user … Do not present the results as satisfying these constraints …"}`.
3. **The safeguard.** Prompt v3 («Never invent … a feature», «`not_listed` means the catalog has no data») and the
   `not_applied` note. There is no check of the answer.
4. **Why it fails.** The three-state model (`yes` / `no` / `not_listed`) exists and works whenever it is *shown*:
   in 4F.2 no `not_listed` state was reported as «нет», and in PA-02 turn 2 of the confirmation run a result with
   `hdmi_2_1: not_listed` produced a correct «в каталоге нет данных». Here the state is not shown. Removing the
   requirement makes the feature disappear from the evidence instead of appearing as **unknown**. The model
   sees its own tool call (with HDMI 2.1), a result with status `ok`, and nothing that contradicts its belief
   except a note buried in `request`. Absent evidence is filled from model knowledge.

   The same gap exists for a feature the *user* names when the Agent calls a tool without passing it
   (an overview `get_tv`, a plain `search_tvs`): the result has no state for it.

### 3.3 Root cause

**A feature that was asked about can be missing from the evidence.** The evidence contract represents "unknown"
only for features that survive into the Core's plan. A removed requirement, or a feature the user named that the
Agent did not pass, has no representation, so the answer generator is free to turn "not shown" into "true".

### 3.4 Fix

| Layer | Change |
|---|---|
| **Response evidence representation** (deterministic) | Every product-returning result carries an explicit state for each *asked* feature: the required features the guard removed, and the registry features the user named in the conversation. They are evaluated by the existing Feature Registry over the returned products and added to each product's `features` (`yes` / `no` / `not_listed`), with one `attribute_not_listed_for_product` gap per feature that is not listed. List results (`search_tvs`, `recommend_tvs`) also get `feature_summary`: for every boolean registry feature, how many of the shown products are `yes` / `no` / `not_listed` — the evidence a statement about "these models" needs. |
| Guard note | `not_applied` says that the removed features were checked, not applied, and where their states are. |
| Prompt (v4) | One general rule: a feature or attribute is stated for a product only if a tool result of the conversation shows it for that product; what a device or use case would benefit from is general knowledge, not a catalog fact. One rule for attributes outside the registry: look them up (`get_tv` with `question`) before saying yes, no or «нет данных». |

- **No HDMI 2.1 special case.** The mechanism is keyed on the closed Feature Registry (14 ids) and on the guard's
  existing one-pattern-per-feature mention table. HDMI 2.1 is one regression fixture among several.
- **Where it is implemented:** at the Agent-facing layer only (`ConsultantTools.call` → a post-processing step in
  `agent_tools` / `agent_payload`). Planning, routing, retrieval, ranking and `evidence.build_evidence` are not
  touched, so the product set and its order cannot change. The step is additive and fail-safe: on any error the
  unannotated result is returned.
- **Not chosen: an answer validator.** A node after the Agent that checks the answer against the tool results is
  the only way to *guarantee* the text. It needs a new n8n node, a second copy of the feature patterns (or a new
  service endpoint), and a repair strategy for a failed check (rewrite, strip a clause, or a second model call).
  Each of these is an architecture change with its own demo failure modes (a correct «HDMI 2.1 не указан»
  misread as a claim). It is the escalation path if the live runs still show the claim (§7).

### 3.5 Regression risk

| Risk | Assessment |
|---|---|
| Product set or order changes | none: the pipeline is not touched; verified by the unchanged DB tests that compare tool results with the 4B/4C order |
| Larger tool results (tokens, latency) | `feature_summary` adds about 12 short entries per list result; asked features add one entry per product |
| The Agent starts listing more features | possible (they are evidenced facts); MINOR at most |
| Extra queries per tool call | two small read-only queries over the returned products only, in the same read-only session |

---

## 4. MVP-2 — invented numeric constraints

### 4.1 Observed failure

PA-08, original run. Turn 2: «ну дюймов 43-50, до сотки где-то» → `{min 43, max 50, max_price 100000}` (correct).
Turn 3: «а что подешевле есть?» → the Agent proposed
`search_tvs {max_price 40000, min_screen_size_inches 40, max_screen_size_inches 50, availability available, sort price_asc, limit 3}`.
The guard left it unchanged, the Core applied `effective_price <= 40000`, and the answer said
«в размере 43-50 дюймов до 40 000 ₽».

### 4.2 Causal path

1. **Origin.** The model turns the comparative «подешевле» into a threshold just below the cheapest product it
   had shown (41 990 ₽ → 40 000), and re-types the carried size bound loosely (43 → 40). Failure mode H02.
2. **Evidence at that point.** The conversation's user messages contain the numbers 43, 50 and the slang «сотки».
   Neither 40 nor 40 000 appears anywhere.
3. **The safeguard.** The semantic guard removes a price, size or refresh-rate argument whose value appears as
   no number in any user message of the conversation. It did exactly this for the same failure mode in 4E.2A
   (invented `max_price 200000` removed) and for 19 invented arguments in the two 4F.2 runs.
4. **Why it fails.** `MessageEvidence.spelled_numbers` is true for turn 2 («сотки» is in the spelled-number
   lexicon), and `_rules` skips **all** numeric rules when any message of the conversation has a spelled number:
   the guard cannot read the value, so it stands down for the whole conversation and for every numeric argument.
   Reproduced offline: with «до 100 тысяч» in place of «до сотки» the same call is `modified`
   (`max_price 40000` and `min_screen_size_inches 40` removed).

### 4.3 Root cause

**An all-or-nothing escape hatch.** One slang or spelled number anywhere in a conversation switches the
deterministic protection off for all later turns, although the number itself is readable («сотка» = 100).

A second, independent weakness is upstream: prompt v3 says «Never invent a budget» but does not say what a
relative word should become, so the model has no sanctioned way to express "cheaper".

### 4.4 Fix

| Layer | Change |
|---|---|
| **Guard / semantic constraint normalization** (deterministic) | The guard reads spelled and slang numbers instead of standing down: Russian numerals with thousand / million multipliers («сто тысяч», «до ста пятидесяти тысяч», «полтора миллиона») and the common slang («сотка», «полтинник», «косарь», «лям»). Their values join the conversation's numbers exactly like digits do. The escape hatch stays for what cannot be read with certainty (a number word outside the grammar, a digit glued to a number word, «пара сотен»): then the numeric rules are off, as today. |
| Prompt (v4) | One rule: relative and vague words are comparisons, not numbers; keep every limit the user stated, unchanged, and answer the comparison by order (`sort`), not by a new bound. |

- The existing mechanism for "cheaper than what was shown" is the order: the same constraints with
  `sort: price_asc`. No new tool argument or intent is introduced.
- Explicit numeric constraints keep working: the guard's rule is unchanged for digits, and a spelled budget is
  now supported by its value instead of by a blanket exemption.

### 4.5 Regression risk

| Risk | Assessment |
|---|---|
| A stated budget is misread and then removed (false removal) | the dangerous direction. The parser accepts only a strict grammar and reports everything else as unreadable, which keeps today's behaviour. Covered by tests over genitive forms, compounds, slang and unreadable cases; the frozen 4D.2E replay and the 22 supplementary guard cases must stay unchanged |
| A carried constraint that the Agent replaced is lost | **real and not removed by the guard.** If the Agent re-types a carried bound with an invented value (`min 40` for the user's 43), the guard removes the invented value and the Core gets a broader request; reproduced offline: `{max_screen_size_inches 50, sort price_asc, limit 3}`, which would list 32″ models. The guard never adds or substitutes a value, by design: a restored value could be one the user has released. The prompt rule is what prevents the re-typing; `not_applied` tells the Agent what was dropped. Measured live in PA-08 and DEMO-04 |
| Report-only conversations | unchanged limitation from 4E.2A: after a turn without a tool call, or a Consultant restart, the guard reports and removes nothing; only the prompt rule applies |

---

## 5. MVP-3 — exact aggregates

### 5.1 Observed failure

PA-15 turn 3: «…И правда, что у Samsung в 2026 году все модели с Dolby Atmos?» The Agent proposed
`get_catalog_stats {stat count, min_price 0, max_price 1000000, availability available, attributes [dolby_atmos]}`.
The result was `counts {available: 66}` for `request.constraints ["is_available = True"]`. The answer:
«В каталоге сейчас 66 доступных моделей Samsung с поддержкой Dolby Atmos». Dolby Atmos is listed for 54 of the
66 available products.

Two further group claims of the same scenario rest on evidence that cannot support them: «у всех OLED … 120 Гц»
from a search filtered to ≥ 120 Hz (turn 1), and «в серии QN70H все модели в наличии» from a search restricted
to available products (turn 3).

### 5.2 Causal path

1. **Origin.** The model wants a feature count and asks for it with an argument the tool does not have.
2. **Evidence at that point.** `get_catalog_stats` counts by typed filters only (panel, category, size, price,
   resolution, refresh rate, availability). **The catalog capability "how many products have feature X" does not
   exist**, and neither does a count scoped to a model family.
3. **The safeguard.** The closed tool schema: an unknown argument is rejected with `invalid_arguments`, which the
   Agent corrects.
4. **Why it fails.** The rejection never happened. The recorded guard decision for this call is `modified`; the
   guard runs only on arguments the tool boundary accepts, and the boundary rejects `attributes` on this tool
   (reproduced offline). So `attributes` was not in the request that reached Python: it was dropped between the
   model and the MCP request, where the n8n MCP Client Tool builds the call from the published input schema.
   The Agent's intent was silently discarded, the Core answered a different question correctly ("how many
   products are available"), and the result names its scope only as a constraint list. The model read the
   answer to the question it believed it had asked.
5. For the two group claims there is no safeguard except the prompt rule about statements over several products:
   a filtered list does not say what it excludes, and the only series-wide lookup (`get_tv` with a family)
   returns at most four members.

### 5.3 Root cause

**A missing structured capability plus a silent argument drop.** Exact feature and series aggregates cannot be
asked for, an attempt to ask is not rejected, and a count does not state that no feature was part of it.

### 5.4 Fix

| Layer | Change |
|---|---|
| **Tool contract** (deterministic) | `get_catalog_stats` with `stat: count` accepts `attributes` (registry features): the result gives `attribute_counts` — per attribute and per availability bucket, how many of the counted products are `yes` / `no` / `not_listed`, summing to the count. It accepts `model` (a family such as QN70H, or a code) to count inside one series. `group_by` gains `refresh_rate_hz`. The argument name `attributes` is the one the model used spontaneously and the one `get_tv` / `compare_tvs` already use for "check these features". |
| **Response evidence representation** (deterministic) | Every count result states `counted` (what was counted, in words) and a fixed note: the counts cover that scope only and are not feature counts; a feature count exists only in `attribute_counts`. |
| Prompt (v4) | One rule: counts and group statements come from `get_catalog_stats`, never from a list; a list is a filtered selection; state the scope; speak about this catalog. |

- Feature counts are computed by the existing Feature Registry over the products the typed filters select: one
  structured read, no semantic search, three states. "54 of 66 list it, for 12 the catalog has no data" is an
  exact answer; "12 do not have it" is not derivable and is not offered.
- **Not done:** per-group feature counts (`attributes` together with `group_by` is rejected with a clear
  message), feature filters on `search_tvs`, percentages (the model divides two exact counts).

### 5.5 Regression risk

| Risk | Assessment |
|---|---|
| The tool schema changes | additive: one new property on one tool, one new enum value; the tool-schema hash changes and is recorded. Existing calls validate and return the same counts (existing DB tests) |
| A larger count result | two short fields; `attribute_counts` only when asked |
| The Agent uses lists for group questions anyway | prompt-dependent; measured live in PA-15 and DEMO-08 |
| Model resolution in the count path | reuses `resolve_model_refs`; an unknown model returns `not_found` with suggestions, never a count |

---

## 6. Change plan

### 6.1 Files

| File | Change |
|---|---|
| `consultant/agent_tools.py` | `get_catalog_stats`: `attributes`, `model`, `group_by: refresh_rate_hz`; asked-feature evidence step in `ConsultantTools.call` |
| `consultant/agent_payload.py` | feature-state encoding helper, `feature_summary`, count scope fields |
| `consultant/semantic_guard.py` | spelled / slang number reader; `not_applied` note |
| `consultant/schemas.py`, `consultant/catalog_repository.py` | `GroupKey.REFRESH_RATE` and its closed SQL mapping |
| `consultant/prompts/agent_system_v4.md`, `consultant/n8n_workflow.py`, `workflows/ai-consultant.json` | prompt v4 (v1–v3 kept for the records); regenerated workflow |
| `tests/` | focused regression tests per root cause; re-anchored record tests (§6.3) |
| `evaluation/` | the 4F.3 runner, demo scenarios, report generator and results |

Not touched: planning, router, retrieval, ranking, evidence, features (registry), query semantics, ingestion,
indexing, the database schema and roles, embeddings, catalog data, Docker compose (except the image tag),
unrelated n8n workflows.

### 6.2 Tests

- **Baseline before any change** (recorded 2026-10-01, HEAD `d05da1f`): `python3 -m pytest -q` → 765 passed,
  117 skipped (DB tests skip without a database); `DOCKER_HOST=unix:///var/run/docker.sock bash
  tests/run_db_tests.sh` → 123 passed; `python -m consultant.n8n_workflow --check` and
  `python -m evaluation.acceptance --check` clean.
- **Focused tests** per root cause (§8), including HDMI 2.1 as a fixture for MVP-1, «до 100 тысяч» → «а что
  подешевле?» for MVP-2, and the 66-vs-54 Dolby Atmos count for MVP-3.
- **Full suites again** after the change, plus the guard replay and the semantic gold set.

### 6.3 Existing tests that pin the 4F.2 frozen state

Three tests assert that the working tree still equals the product that was run in 4F.2. They are correct for a
measurement phase and cannot hold in a remediation phase:

| Test | Pinned | Re-anchored to |
|---|---|---|
| `test_acceptance_results::test_the_rubric_and_the_runtime_sources_are_the_ones_that_were_run` | runtime file hashes == 4F.2 manifest | the manifest and the design document agree with each other; files this phase did not change still match |
| `test_acceptance_scenarios::test_document_records_the_current_frozen_file_hashes` | 4F.1 §1.2 hashes == working tree | 4F.1 §1.2 hashes == the 4F.2 run manifest |
| `test_agent_tools_unit::test_system_prompt_states_the_required_rules` | prompt file name `v3`, length limit | `v4`, new limit; every v3 rule still asserted |

The 4F.2 result files, the 4F.1 design sections and `acceptance_scenarios.json` are not edited, and
`acceptance_report --check` must still report them as current.

### 6.4 Deployment

Only the Consultant: a new image (tag `4f3`) through the existing compose project, and the Consultant workflow
`4d8mXFWGpS5P4t1L` updated to the regenerated artifact (prompt v4), in that order, following the
[runbook](../deploy/consultant/README.md). n8n, postgres, redis and traefik are not restarted. Rollback: image
`4e2a` and the workflow backup taken by `n8n-tool` at the update.

---

## 7. MVP demo gate

This gate is separate from the 4F.1 release rule and does not reuse its thresholds.

**Targeted live regression** (fresh sessions, the 4F.2 turns unchanged): PA-02, PA-04, PA-08, PA-15.

| Scenario | MVP-critical defect that must be gone |
|---|---|
| PA-02, PA-04 | no unsupported HDMI 2.1 factual claim |
| PA-08 | «подешевле» creates no invented numeric budget |
| PA-15 | exact counts and group claims rest on correct structured evidence |

**Demo suite:** 6–8 realistic conversations (`DEMO-01` … `DEMO-08`), full transcripts committed.

**`MVP DEMO READY` requires all of:** 0 invented product or model codes; 0 invented prices; 0 invented
availability; 0 unsupported factual claims that materially affect a recommendation; 0 invented numeric user
constraints; 0 obviously incorrect exact aggregates; no hard-constraint violations; multi-turn constraint
changes work; at least 6 successful demo conversations; no infrastructure or runtime error during the demo
suite. Wording issues and non-material qualitative phrasing are noted and do not block.

**Escalation.** If a targeted scenario still shows its defect after the fixes above, the next layer is named in
the section of that target (for MVP-1: answer validation) and is a decision for the project owner, not an
automatic next step.
