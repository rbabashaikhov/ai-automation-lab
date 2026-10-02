# Phase 4F.3 — MVP Demo Hardening

Status: **complete — `4F.3 MVP DEMO HOLD — FURTHER HARDENING REQUIRED`.** MVP-1 and MVP-3 are fixed as measured.
MVP-2 is fixed where it was deterministic (no invented number reaches the Core) and not in the answer text: the
model still words its own removed limit («до 50 000 ₽») as if the user had set it. §3–§7 are the analysis and
plan written before any code change; §8–§14 record what was built, measured and decided. §15 records the
follow-up hotfix 4F.3A (reject instead of remove): built, verified live, failed, reverted —
`4F.3A MVP DEMO HOLD — NUMERIC CONSTRAINT BUG REMAINS`.

Phase 4F.2 ended with `4F.2 HOLD — PRODUCT ACCEPTANCE FAILED`. That result stands and is not changed by this
phase (§2). Phase 4F.3 has a narrower target, set by the project owner: a **demo-ready MVP for portfolio and
client demonstration**. It removes three defects that would undermine trust in a live demo and leaves every
other 4F.2 finding as a known limitation.

| Artifact | Contents |
|---|---|
| This document | scope, root causes, fix design, regression risks, test and deployment plan, MVP gate (§1–§7); implementation, iterations, tests, deployment, result, limitations (§8–§14); the 4F.3A hotfix and its result (§15) |
| [`evaluation/results/phase_4f_3_demo/`](../evaluation/results/phase_4f_3_demo/PHASE_4F_3_MVP_DEMO_REPORT.md) | the 4F.3 report, evidence, review, measurements and transcripts |
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
framework. The 4F.2 classes outside the three targets stay as known limitations (§13).

**What must not regress** (it held in all 90 turns of 4F.2): no invented model code, price or availability;
no product that breaks an active hard constraint; multi-turn constraint memory; replaced and released
constraints; `not_listed` never reported as «нет».

## 2. Relation to Phase 4F.2

| Statement | Status |
|---|---|
| `4F.2 HOLD — PRODUCT ACCEPTANCE FAILED` | stands; the 4F.2 files are not edited |
| The product is accepted as production-ready under the 4F.1 rule | **no** — not claimed by this phase |
| The product is demo-ready as a portfolio MVP | decided by the MVP gate of §7, after the live runs: **not yet** (§12) |

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

> **As built (§8.1):** the per-product state for asked features and the gap shipped. `feature_summary` and both
> prompt-v4 rules were built, measured and removed: they made answers worse (§9). What replaced them is a caveat
> note in the result when an asked feature is listed for none of the returned products.

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

> **As built (§8.2):** the number reader shipped. The prompt rule was part of prompt v4 and was removed with it.

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

> **As built (§8.3):** the tool contract and the scope fields shipped, plus the listed values of a counted feature
> and a `same_value_for_all` flag (found necessary in the live runs, §9). The prompt rule was removed with
> prompt v4; the tool descriptions carry the instruction instead.

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

*The plan as written before the changes. Differences as built: no prompt v4 and no `feature_summary` (§8, §9);
the third test of §6.3 is back on prompt v3; the final image tag is `4f3f` (§11).*

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
- **Focused tests** per root cause (§10), including HDMI 2.1 as a fixture for MVP-1, «до 100 тысяч» → «а что
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

---

## 8. Implementation as built

The prompt is **unchanged** (`agent_system_v3.md`, the 4F.2 hash). All three fixes are in what the tools accept and
return and in the guard's number reader. Planning, routing, retrieval, ranking and `evidence.build_evidence` are
not touched: the product set and its order are the same as in 4F.2.

### 8.1 MVP-1 — a feature that was asked about is always in the evidence

`agent_tools.add_feature_evidence`, called from `ConsultantTools.call` after the tool has run:

- **Asked features** = required features the guard removed in this call + registry features the user named in the
  conversation (the guard's existing mention table). Nothing else: when no feature was asked, the result is
  byte-identical to the 4F.2 one.
- For every returned product each asked feature gets its Feature Registry state in `features`
  (`yes` / `no` / `not_listed`, with the value where the feature has one). `request.features_checked` lists them.
- One `attribute_not_listed_for_product` gap per asked feature that is not listed, naming the products.
- If an asked feature is listed for **none** of the returned products, the result's confidence drops from `strong`
  to `partial` and `confidence_notes` gets: *"The catalog lists {features} for none of these products: unknown,
  neither 'yes' nor 'no'. Do not say that they have it or support it; say that the catalog has no data."*
- Fail-safe: on any error the unannotated tool result is returned.

There is no HDMI 2.1 branch anywhere: the step runs over the 14 registry ids, and a test runs the same assertion
for each of them.

### 8.2 MVP-2 — the guard reads spelled and slang numbers

`semantic_guard.spelled_numbers` (guard version `semantic-guard-4f3-v1`): Russian numerals in their case forms with
thousand / million multipliers («сто тысяч», «ста пятидесяти тысяч», «полтора миллиона») and slang («сотка»,
«полтинник», «косарь», «лям»). A value that is read joins the conversation's numbers as itself, ×1 000 and
×1 000 000, exactly as a digit does. A number word that cannot be read with certainty («пара сотен», «несколько
тысяч») keeps the old behaviour: numeric rules off for that conversation. The guard still only removes; it never
adds or replaces a value. The `not_applied` note now also says that stated limits must be passed exactly as stated.

### 8.3 MVP-3 — exact counts for features and series

`get_catalog_stats` with `stat: count`:

| Argument | Result |
|---|---|
| `attributes: [feature…]` | `attribute_counts[feature][bucket] = {yes, no, not_listed}` over the counted products, per availability bucket, summing to the count. For a feature that carries a value (refresh rate, sound power, depth) the same entry has `values` (`{value: products}`, or min / max above 12 distinct values) and `same_value_for_all` |
| `model: "QN70H"` | the count inside one family or for one code; an unknown model returns `not_found`, never a count |
| `group_by: "refresh_rate_hz"` | new group key |
| every count | `counted` (the scope in words) and `scope_note`: the counts cover that scope only; a feature count exists only in `attribute_counts` |

`attributes` together with `group_by`, or with a `stat` other than `count`, is rejected with a message. The
`search_tvs` description says that a list is a filtered selection and points to `get_catalog_stats` for counts and
«все ли …» questions. Tool-schema hash: `f215b7d33c1495f2` (4F.2: `4645dac666b7ce5e`).

### 8.4 Files

| File | Change |
|---|---|
| `consultant/agent_tools.py` | `add_feature_evidence`, `_asked_features`; `get_catalog_stats`: `attributes`, `model`, `refresh_rate_hz`, `_attribute_counts`, `_value_distribution` |
| `consultant/agent_payload.py` | `feature_view`, `stats_payload` (`counted`, `attribute_counts`, `scope_note`), the two notes |
| `consultant/semantic_guard.py` | the number reader; `MessageEvidence` uses it; the `not_applied` note |
| `consultant/schemas.py`, `consultant/catalog_repository.py` | `GroupKey.REFRESH_RATE` and its closed SQL mapping |
| `consultant/n8n_workflow.py`, `workflows/ai-consultant.json` | the OpenAI credential's display name as n8n now reports it (same credential id); nothing else |
| `deploy/consultant/` | image tag, `mvp_probe.js`, runbook lines |
| `tests/test_mvp_hardening.py`, `tests/test_mvp_hardening_db.py`, `tests/test_mvp_demo.py` | new |
| `tests/test_acceptance_results.py`, `tests/test_acceptance_scenarios.py`, `tests/test_agent_tools_unit.py` | re-anchored (§6.3) |
| `evaluation/mvp_demo.py`, `evaluation/mvp_demo_scenarios.json`, `evaluation/results/phase_4f_3_demo/` | the 4F.3 harness, scenarios and results |
| `evaluation/results/guard_replay_4e2.json` | regenerated: the guard version string; all 35 + 22 rows identical |

---

## 9. Iterations and measurements

One recorded conversation is weak evidence: at temperature 0 gpt-4.1-mini answers the same first turn differently
from session to session. From the second build on, every change was measured as a rate over 10–20 fresh sessions,
and the two variables (prompt, evidence) were separated. All answers are in
[measurements.json](../evaluation/results/phase_4f_3_demo/measurements.json).

**PS5 question («Посоветуйте телевизор под PS5…»), 12–20 sessions each:**

| Build | Prompt | Evidence | HDMI 2.1 attributed | Price error | Lists all 8 |
|---|---|---|---|---|---|
| 4F.2 (`4e2a`) | v3 | 4F.2 | **20/20** | 0/20 | 2/20 |
| `4e2a` | v4 | 4F.2 | 20/20 | 1/20 | 1/20 |
| `4f3a` | v4 | per-product state + `feature_summary` | 5/12 | 1/12 | 4/12 |
| `4f3b` | v4 + phrasing rule | + `price_text` | 1/12 | 0/12 | 8/12 |
| `4f3c` | v4 | + `all_yes` / `not_listed_for_all` lists | 0/20 | 2/20 | **13/20** |
| `4f3d` | v4 | asked features only | 1/20 | **6/20** | 9/20 |
| `4f3d` | v3 | asked features only | 1/20 | 0/20 | 3/20 |
| `4f3e` candidate | v3 | asked features only + caveat note | 0/20 | 0/20 | 3/20 |
| `4f3e` deployed | v3 | the same | **0/20** | 0/20 | 3/20 |

What the table says:

1. **The prompt was not the lever.** Prompt v4 alone left the claim at 20/20. The rule existed in v3 already; the
   model did not lack an instruction, it lacked evidence.
2. **More evidence was not better.** A summary over all registry features gave the model a list to recite: it began
   listing every shown product, and a price drifted between neighbouring products (a real price of another model).
   Each addition that fixed one thing moved another.
3. **Prompt v4 itself caused the price errors**: the same evidence gives 6/20 with v4 and 0/20 with v3.
4. The smallest design is the one that held: say what is unknown only for what was asked, and say it once in the
   result's caveats, where the model already looks for reasons to hedge.

**Other measurements on the final design:**

| Question | Result |
|---|---|
| OLED 65″ for PS5 under 300 000 ₽ (PA-04 turn 1), 10 sessions | HDMI 2.1 attributed 0/10 |
| «А телевизоры до 50 тысяч — они все на 60 Гц?» | values beside the counts: false «все на 60 Гц» 3/10; nested in the count entry: 3/20; with `same_value_for_all`: **0/20** |
| «…все модели с Dolby Atmos? Сколько таких в каталоге?», 10 sessions | exact counts 10/10 (62 of 75; 54 of 66 available) |
| Vague opener «…не огромный…» (PA-08 turn 1), 20 sessions | the guard removes the invented `max 55″` 20/20; the answer still says «до 55 дюймов»: 11/20 on 4F.2, 15/20 final |
| the same with a caveat note for removed numeric limits (experiment) | 14/20 — no effect, not adopted |
| «55 дюймов до 100 тысяч…» → «А есть что-то подешевле?», 12 sessions | the guard removes the invented `max_price 50000` 12/12; the answer still says «до 50 000 ₽»: **12/12** |

The last two rows are the open defect (§12).

---

## 10. Tests

| Command | Baseline (HEAD `d05da1f`) | Final |
|---|---|---|
| `python3 -m pytest -q -p no:cacheprovider` | 765 passed, 117 skipped | **893 passed, 142 skipped** |
| `DOCKER_HOST=unix:///var/run/docker.sock bash tests/run_db_tests.sh` | 123 passed | **148 passed** |
| `python3 -m consultant.n8n_workflow --check` | clean | clean |
| `python3 -m evaluation.acceptance --check` | clean | clean |
| `python3 -m evaluation.acceptance_report --check` (4F.2 results untouched) | clean | clean |
| `python3 -m evaluation.mvp_demo --check` | — | clean |
| `python3 -m evaluation.guard_replay` | 35/35, 22/22 | 35/35, 22/22; nothing invented by the guard |
| `python3 -m evaluation.semantic_eval` | 30/30 | 30/30 |

Skipped unit tests are the DB-backed ones, which run in the second command.

Focused regressions (`tests/test_mvp_hardening.py`, `tests/test_mvp_hardening_db.py`):

- **MVP-1.** A removed required feature is reported `not_listed` per product, with the gap and the caveat note —
  HDMI 2.1 as one fixture and the same assertion generically over all 14 registry features; the note appears only
  when the feature is unknown for *all* returned products; a result is untouched when nothing was asked; a failure
  inside the step returns the plain result; a feature the user named is reported on an overview lookup.
- **MVP-2.** «до 100 тысяч» → «а что подешевле?» with an invented `max_price`: removed; the recorded PA-08 call
  after «до сотки»: `max_price 40000` and `min 40` removed, the stated 43–50 / 100 000 kept; explicit numbers
  («до 100 тысяч», «65 дюймов», «не дороже 80 000») kept; the numeral tables in their case forms; unreadable number
  words keep the numeric rules off; no relative word (подешевле, не огромный, небольшой, подороже, побольше, чуть
  дешевле, бюджетный, не самый дорогой) yields a number.
- **MVP-3.** Feature counts sum to the count and differ from the availability count on a fixture built for it;
  series scope; unknown series → `not_found`; `values` and `same_value_for_all`; rejected combinations; `counted`
  and `scope_note` on every count.
- **Harness** (`tests/test_mvp_demo.py`): each supporting check is validated on recorded evidence (it flags exactly
  the 4F.2 HDMI claims, the 40 000 ₽ ceiling, the 66 Dolby Atmos count, the run-1 «все на 60 Гц», the run-2 price
  of another product, and the two voiced limits of the final runs); the gate decision on synthetic runs; the
  committed results follow from their inputs.

---

## 11. Deployment

Only the `samsung-consultant` service, by the existing procedure
([runbook](../deploy/consultant/README.md)): build from the committed tree, compare the files in the image with the
repository, `docker save` / `load`, a smoke container on the internal network with the three probes, compose `up`
for the one service, the same probes on production.

| Image | Revision | Deployed (UTC) | Prompt in the workflow | Status |
|---|---|---|---|---|
| `4f3` | `9ca118b` | 2026-10-01 12:16 | v4 | superseded |
| `4f3a` | `2a3784b` | 12:34 | v4 | superseded |
| `4f3b` | `0856ba4` | 12:54 | v4 | superseded |
| `4f3c` | `98e663c` | 13:01 | v4 | superseded |
| `4f3d` | `b4a55cb` | 13:14 | v4, then v3 restored at 13:57 | superseded |
| `4f3e` | `751473f` | 14:21 | v3 | superseded |
| **`4f3f`** | **`afc8678`** | **14:56** | **v3** | **in production** |

- Final image id `sha256:f9e26b657efd58f7f8b436462ea57edccc340559d74602bac3763e72e57528d0`; 23 runtime files in the
  container identical to the repository; healthy, no published port, read-only root FS.
- The Consultant workflow `4d8mXFWGpS5P4t1L` was updated six times with `n8n-tool` (a backup before each, kept under
  `tools/n8n-tool/backups/samsung-ai-consultant/`); it is inactive and diff-identical to `workflows/ai-consultant.json`,
  which carries prompt v3.
- **The first update reported a failed read-back check.** The only difference was the display name of the OpenAI
  credential: n8n returned `OpenAI account samsung-ai` where the file said `OpenAI account`, for the same credential
  id. The credential had been renamed in n8n outside this work; nothing about it was changed here. The generator's
  label was aligned and later updates verify cleanly.
- Seven builds in one afternoon is more deployment activity than the plan foresaw (one). Each went through the
  full procedure; production served a superseded build between them. The workflow stays inactive, so no end user
  was exposed to an intermediate build.
- n8n, PostgreSQL, Redis and Traefik were not restarted. Side containers used for baseline and candidate
  measurements ran next to production on the internal network and were removed. The superseded images and the
  `compose.yml.<tag>.bak` files remain on the VPS. *(Removed at project closure, 2026-10-02:
  [PROJECT_CLOSURE.md](PROJECT_CLOSURE.md).)*

---

## 12. Result

Final runs on `4f3f`, fresh sessions, gpt-4.1-mini: targeted regression `20261001T1457Z`, demo suite
`20261001T1459Z`. Report: [PHASE_4F_3_MVP_DEMO_REPORT.md](../evaluation/results/phase_4f_3_demo/PHASE_4F_3_MVP_DEMO_REPORT.md).

| Scenario | 4F.2 defect | Gone | Conversation |
|---|---|---|---|
| PA-02 | «с поддержкой HDMI 2.1» | yes — «в каталоге нет данных о поддержке HDMI 2.1» | PASS |
| PA-04 | the same | yes | PASS |
| PA-08 | «подешевле» → `max_price 40000` in the Core and in the answer | yes — no number invented, nothing reached the Core | **FAIL** on turn 1: «до 55 дюймов» for «не огромный» |
| PA-15 | 66 available presented as 66 with Dolby Atmos; universal claims from lists | yes — 62 of 75; 15/15 OLED from attribute counts; «50 или 60 Гц» | PASS |

Demo suite: **7 of 8 pass** (DEMO-01, 02, 03, 05, 06, 07, 08). DEMO-04 fails on turn 2: «подешевле» → «нет
телевизоров … до 50 000 ₽».

| MVP gate criterion | Met |
|---|---|
| the targeted 4F.2 defects are gone | yes |
| 0 invented model codes, prices, availability | yes |
| 0 unsupported factual claims that affect a recommendation | yes |
| **0 invented numeric user constraints** | **no** — two answers state a limit the user never gave |
| 0 incorrect exact aggregates | yes |
| no hard-constraint violations; multi-turn changes work | yes |
| at least 6 successful demo conversations | yes (7) |
| no infrastructure or runtime errors | yes |

**`4F.3 MVP DEMO HOLD — FURTHER HARDENING REQUIRED`**

The hold has one cause. The guard does its part: in 32 of 32 measured sessions the invented number was removed and
the Core never filtered by it, so the *products* are right. But the model wrote the number into its tool call
because it had already decided on it, and it then describes the result in those terms. In the «подешевле»
conversation this happens every time (12/12) — it is what a client would see in a demo of exactly the feature this
phase was meant to fix. Calling that ready would be wrong.

---

## 13. Known limitations

- **Phase 4F.2 stands:** `4F.2 HOLD — PRODUCT ACCEPTANCE FAILED`. The 4F.1 release gate was not re-run and is not
  met. Qualitative language, bright-room and best-for-movies reasoning, ranking and the MINOR issues are untouched.
- **Invented numeric limits are voiced** (above).
- **Rates, not guarantees:** 0/20 is not a proof of zero.
- **No answer-level check:** a claim about a feature nobody asked about is governed by the prompt only.
- **Three tool calls per turn:** a question about four models loses one lookup (DEMO-02).
- **«Подешевле» answered from memory** respects the stated limits but is not necessarily cheapest-first (PA-08 turn 3).
- **Wording:** lists longer than three; a feature id once; «не у всех» where the exact statement is «listed for N,
  no data for M».
- **Counts** exist for the 14 registry features and structured columns only.
- **Spelled numbers:** a closed grammar; anything outside it switches the numeric rules to report-only, as before.

---

## 14. Next step (for the project owner; not started)

The remaining defect is in the answer text, so the remaining options are the two layers this phase did not touch:

| Option | What it is | Cost / risk |
|---|---|---|
| **Reject instead of remove** | When the guard finds an invented numeric limit, the tool returns an error that names it and asks for the call without it, instead of silently running without it. The model then has to re-plan without the number | Small, inside the Consultant. Uses one of the three tool calls of the turn. Changes the guard's "never blocks" property from 4E.2, so the 35 + 22 replay rows and the live rates must be re-measured |
| **Answer check** | A deterministic check after the Agent: a price or size limit in the answer that no user message states and no product has → strip the clause or regenerate once | Guarantees the text. Needs a node in the n8n workflow and a repair strategy; the design choice §3.4 deferred |

Either should be measured the way §9 was: a rate over fresh sessions on the two conversations that fail now, plus
the four targeted scenarios, before any claim of readiness.

> **Update, 2026-10-02:** the first option was built and verified live as hotfix 4F.3A. It did not remove the
> defect and was reverted (§15).

---

## 15. Phase 4F.3A — «reject instead of remove»: built, measured, reverted

Status: **`4F.3A MVP DEMO HOLD — NUMERIC CONSTRAINT BUG REMAINS`** (2026-10-02). The first option of §14 was built as
a hotfix, deployed and verified live. The rejection works; the defect does not go away. By the hotfix's own stop
rule nothing was retuned, the demo suite was not run on that build, and both the code and production are back at
the Phase 4F.3 state. §12 stands unchanged: 7 of 8 demo conversations, image `4f3f`.

### 15.1 What was built (commit `07d935b`, reverted by `c6993aa`)

The guard's decision was not changed. At the tool boundary (`ConsultantTools.call`) a decision that removes a
numeric hard constraint became a rejection: the call is not run, and the Agent gets

```json
{"status": "invalid_arguments",
 "errors": ["unsupported_numeric_constraint: max_price=50000 was not stated by the user in this conversation.",
            "This call was not run. Call the tool again without each constraint named above, or with the exact value the user stated for it, if any. Do not substitute another number. The user set no such limit, so the answer must not state one. Keep the other arguments."],
 "unsupported_numeric_constraints": [{"argument": "max_price", "value": 50000}]}
```

Generic over the six numeric arguments of the tools (`min_price`, `max_price`, `screen_size_inches`,
`min_screen_size_inches`, `max_screen_size_inches`, `min_refresh_rate_hz`); no value or phrase special-cased. An
unmentioned required feature was still removed and reported; report-only, skipped and fallback decisions ran as
before; a rejection counted towards the cap of three calls per message. Prompt v3, the model, the workflow,
retrieval and ranking were not touched. Prompt v3 already says what to do with the result: «If a result is
`invalid_arguments`, correct the arguments yourself and call the tool again once».

Tests at `07d935b`: `pytest` 923 passed, 143 skipped (baseline 893 / 142); DB suite 149 passed (148); guard replay
35/35 and 22/22 with identical rows (only the guard version string changed); semantic gold set 30/30;
`n8n_workflow --check`, `mvp_demo --check`, `acceptance --check`, `acceptance_report --check` clean.

### 15.2 Deployment

| Step (UTC, 2026-10-02) | State |
|---|---|
| before | `samsung-consultant:4f3f`, revision `afc8678`, started 2026-10-01T14:56:31Z, healthy |
| 09:57:22 deploy | `samsung-consultant:4f3g`, revision `07d935b`, built from the committed tree; 23 runtime files identical in the image, in the container and in the repository; smoke container and production gave the same probe output (`mvp_probe.js`: the invented `max_price 40000` / `min 40` → `invalid_arguments`, stated limits kept) |
| 09:59–10:01 | live verification, run `20261002T0959Z` |
| 10:07:08 rollback | `samsung-consultant:4f3f`, image id `sha256:f9e26b65…` (the one of §11), revision `afc8678`, healthy; 23 runtime files identical to the repository at `c6993aa`; probe output as in Phase 4F.3 |

Only the `samsung-consultant` service was recreated, twice. n8n, PostgreSQL, Redis and Traefik were not restarted
(same `StartedAt`). The Consultant workflow was not updated. One temporary inactive driver workflow was created for
the run and deleted after it. No port, role, schema or data change. Image `4f3g` and `compose.yml.4f3g.bak` remain
on the VPS, unused; `4f3g` is not a rollback target. *(Removed at project closure, 2026-10-02:
[PROJECT_CLOSURE.md](PROJECT_CLOSURE.md).)*

### 15.3 Live verification (fresh sessions, gpt-4.1-mini, temperature 0)

| Turn | User | Agent's proposal | Guard | Corrected call | Answer |
|---|---|---|---|---|---|
| PA-08 turn 1 | «…хочу норм телек в спальню, не огромный…» | `recommend_tvs {max_screen_size_inches 55, use_cases [movies]}` | rejected | **none** | «…я бы порекомендовал телевизор с диагональю **до 55 дюймов**… скажите, пожалуйста, какой у вас бюджет…» — no products |
| PA-08 turn 2 | «ну дюймов 43-50, до сотки где-то» | `{min 43, max 50, max_price 100000, use_cases [movies]}` | unchanged | — | three models within the stated limits |
| PA-08 turn 3 | «а что подешевле есть?» | `{min 43, max 50, max_price 45000, …}` | rejected | **none** | «Вы хотите телевизор… бюджетом **до 45 тысяч рублей**? Подтвердите, пожалуйста…» — no products |
| PA-08 turn 4 | «…этот второй — он в наличии? ссылку дайте» | no tool call | — | — | correct, from the turn-2 result |
| DEMO-04 turn 1 | «Нужен телевизор 55 дюймов до 100 тысяч…» | `{screen_size_inches 55, max_price 100000, use_cases [movies]}` | unchanged | — | four models within the limits |
| DEMO-04 turn 2 | «А есть что-то подешевле?» | `search_tvs {screen_size_inches 55, max_price 50000, availability available}` | rejected | **none** | «Вы не указывали в запросе бюджет **до 50 тысяч**, поэтому я не могу искать телевизоры с таким ограничением. Пожалуйста, уточните, до какой суммы…» |
| DEMO-04 turn 3 | «…самый дешёвый из них сейчас в наличии?…» | no tool call | — | — | correct, from the turn-1 result |

| Criterion | PA-08 | DEMO-04 |
|---|---|---|
| no invented number reaches the Core | yes (2 of 2 rejected) | yes (1 of 1 rejected) |
| no invented number in the tool arguments the model proposes | no | no |
| the rejected call is followed by a corrected one | no (0 of 2) | no (0 of 1) |
| no invented number in the final text | no: «до 55 дюймов», «до 45 тысяч рублей» | no: «бюджет до 50 тысяч» |
| «подешевле» works as a relative request | no: a question instead of an answer | no: a question instead of an answer |
| **Result** | **FAIL** | **FAIL** |

Evidence: [`evidence_4f3a_PA-08.json`](../evaluation/results/phase_4f_3_demo/evidence_4f3a_PA-08.json),
[`evidence_4f3a_DEMO-04.json`](../evaluation/results/phase_4f_3_demo/evidence_4f3a_DEMO-04.json) — per turn: user
turn, answer, proposed arguments, the guard's logged decision, the result, memory evidence, automated checks. They
were written by `mvp_demo collect` as it was at `07d935b` (it read a rejected number like a removed one; the check
`unapplied_limits_stated` flags all three answers). Every execution succeeded; no tool returned `error`.
Transport events, none of them a re-run of a conversation: one TLS timeout while extracting the PA-08 trace
(retried by the tooling); the first launch of DEMO-04 ended in the SSH transport (rc 255) before anything executed —
the Consultant log has no request of that session — and it was then launched once; two TLS timeouts of the n8n API
during `collect`.

### 15.4 What the result says

- **The deterministic half is solved and was before:** an invented number does not reach the Core, removed or rejected.
- **The rejection does not make the model re-plan.** In 3 of 3 rejected turns it made one tool call and no second
  one. It read the validation error as something to put to the user, which the prompt forbids («never ask the user to
  fix tool arguments»), and repeated the number while doing so. The number moved from a claim («нет телевизоров до
  50 000 ₽», §12) to a question («бюджетом до 45 тысяч рублей?»); it is still in the answer, and the answer now has
  no products.
- Three turns in two conversations of one run are not a rate (§9). They are enough for the stop rule, which asked
  for a pass in both scenarios on the first attempt, not for a measurement.
- Not tried, by the stop rule: another wording of the rejection, a prompt rule, any second pass.

### 15.5 Decision

**`4F.3A MVP DEMO HOLD — NUMERIC CONSTRAINT BUG REMAINS`**

The demo suite was not run on `4f3g`. Leaving `4f3g` in production would have kept a build with a failed
verification whose behaviour in the other seven demo conversations is unmeasured (a rejected number turns an answer
into a question), so production and the repository were returned to the state §12 describes. Of the two options of
§14, «reject instead of remove» is now measured and closed in this form; the answer check remains, as a decision
for the project owner.
