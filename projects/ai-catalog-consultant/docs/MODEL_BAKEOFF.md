# Model Bake-off Gate — Samsung AI Consultant

Status: **`MODEL BAKE-OFF ACCEPT — KEEP GPT-4.1-MINI`** (§14). Measurement only: nothing in the
runtime was tuned, the production model was never switched, and nothing is merged or deployed.

Question: is another model materially better than the current `gpt-4.1-mini` for this Consultant,
enough to justify changing the release model? **No.** On the same frozen runtime the baseline had the
best product correctness in both stages; `gpt-4o-mini` is cheaper but regresses tool-call validity;
`gpt-4.1` writes the most useful answers but adds claims from its own knowledge, costs about 5 times
more per turn and is limited to 30 000 tokens per minute on this account.

Artifacts:

| File | Contents |
|---|---|
| [`evaluation/results/model_bakeoff.json`](../evaluation/results/model_bakeoff.json) | frozen configuration with sha256, per-model metrics, per-turn verdict table, infrastructure events |
| [`evaluation/results/model_bakeoff_conversations.md`](../evaluation/results/model_bakeoff_conversations.md) / [`.json`](../evaluation/results/model_bakeoff_conversations.json) | **Conversation Audit Log**: all 246 scored turns of all three models — user message, final answer, tool, arguments the model proposed, guard decision, arguments the Core received, compact tool result, latency, tokens, estimated cost, automated and manual verdict. No model reasoning is recorded |
| [`evaluation/results/model_bakeoff_manual.json`](../evaluation/results/model_bakeoff_manual.json) | reviewer rubric and the verdict, note and counts for every turn |
| [`evaluation/model_bakeoff.py`](../evaluation/model_bakeoff.py), [`bakeoff_report.py`](../evaluation/bakeoff_report.py), [`bakeoff_cases.json`](../evaluation/bakeoff_cases.json) | runner, report builder, supplementary cases |

---

## 1. Git / baseline state

| Item | Value |
|---|---|
| Starting SHA | `main == origin/main == d2843ea9d7e46c5cff1d2610d19b9c6343246f77`, clean tree, no drift after `git fetch` |
| Feature branch | `feature/samsung-ai-consultant-model-bakeoff` |
| Commits | `ee0ff2a` — pre-registration (case set, stage plan, rules, model parameterization), committed **before any candidate run**; then the results commit that adds this document |
| Working tree | clean after the results commit |
| `main` / `origin/main` | untouched (`d2843ea`); nothing pushed, nothing merged |

## 2. Frozen experiment configuration

Only the chat model id changed. It was replaced in the temporary driver's **inline copy** of the
Consultant workflow (`agent_live.with_model`); the deployed workflow was never edited. Every turn's
trace records what n8n actually sent to the provider: 246/246 scored turns show the intended model,
`temperature 0`, the same API mode, and no max-tokens setting.

| Constant | Value |
|---|---|
| Prompt | `consultant/prompts/agent_system_v3.md`, sha256 `c0140353…` (same as 4E.2) |
| Workflow | `workflows/ai-consultant.json`, sha256 `67eb55d6…`, 7 nodes with `Prior turns`; deployed `4d8mXFWGpS5P4t1L` diff-identical before and after, inactive |
| Tool schemas | sha256-16 `4645dac666b7ce5e` (container == repository == 4E.2); five closed read-only tools |
| Semantic guard | `semantic_guard.py` sha256 `3df93354…` (container == repository); image `samsung-consultant:4e2a` |
| Temperature | 0 for every model (supported by all three; no substitution needed) |
| Max tokens | not set (provider default) for every model |
| Memory | window 6, per `sessionId`; one session per case and per model |
| Tool-call cap | 3 per user message, enforced by the Consultant; Agent max iterations 4 |
| Catalog | 75 products / 66 available, md5 `7c1acf94…`, 4151 spec rows, 75 documents, 514 chunks — identical before and after |
| Frozen dataset | `evaluation/agent_cases.json`, sha256 `3f815082…` (byte-identical to 4D.2E / 4E.2) |
| Supplementary dataset | `evaluation/bakeoff_cases.json`, sha256 `9b3e6899…` (new; written and committed before the first run) |
| Scorer | `evaluation/agent_eval.py`, sha256 `64604e7d…` (unchanged since 4E.2) |
| Measurement | `agent_live` traces: end-to-end latency of the Consultant sub-execution; n8n token estimates; list prices |

Rules registered in `ee0ff2a`: the stage plan, the stage-1 elimination rule and the execution-failure
rule (`model_bakeoff.py`).

**Runner changes after the freeze** (transport and pacing only; the stage plan, cases, prompt, tools,
guard, memory and scorer are unchanged — `model_bakeoff.json` lists both hashes):

- optional pauses between cases and between turns, off by default (§4, rate limit);
- ssh keepalive, so that a network outage stops a run instead of hanging it.

The owner replaced the key inside the existing n8n credential `OpenAI account` before the runs (the
account had no credits). The credential id and the workflow reference did not change; this gate
never read or printed a key.

## 3. Models tested

| Model id (as requested and as sent) | Provider | Runtime parameters | List price, USD per 1M tokens (in / out) |
|---|---|---|---|
| `gpt-4.1-mini` (baseline) | OpenAI | temperature 0, Responses API, max tokens unset | 0.40 / 1.60 |
| `gpt-4o-mini` | OpenAI | the same | 0.15 / 0.60 |
| `gpt-4.1` | OpenAI | the same | 2.00 / 8.00 |

Prices were read from the provider's pricing page on the day of the run. `gpt-4o` was not tested
(§14: the result is not inconclusive).

## 4. Evaluation coverage

| Stage | Cases / turns | Multi-turn cases | Models |
|---|---|---|---|
| 1 — fixed subset | 29 / 38: 19 of the frozen 42 cases (21 turns) + all 10 supplementary cases (17 turns) | 7 | all three |
| 2 — full frozen evaluation | 42 / 44 | 1 | all three (nobody was eliminated) |
| Total scored | 82 turns per model, **246 turns** | | |

- **Why supplementary cases.** The frozen set has one multi-turn case and no explicit-HDMI-2.1, budget
  override or budget release case. The 10 supplementary cases use conversations already accepted in
  4E.2 / 4E.2A (`guard_cases.json`, scenarios A–F, the targeted live tests); only their expected-answer
  labels are new. `agent_cases.json` is unchanged.
- **Required special cases:** "для PS5" (`rec-gaming`, `bake-mt-*`), explicit HDMI 2.1
  (`bake-ps5-hdmi-explicit`, `bake-budget-hdmi-explicit`), invented-budget risk (`followup[2]`,
  `bake-movies-no-budget`, `bake-large-no-size`), budget retention (`bake-mt-carried-budget`,
  `bake-mt-budget-retention`), budget override and release (`bake-mt-budget-override`), no-match
  (`rec-impossible`), comparison (`compare-*`), `thin_wall`, price-sensitive recommendation
  (`rec-budget-gaming`), product-code questions (`get-tv-*`), catalog list / aggregate (`search-*`,
  `stats-*`).
- **Stage-1 decision** (rule registered before the run): manual 36 / 35 / 35 of 38. No model was 3
  turns below the best; no model fabricated a model code or a price; lost-constraint turns were
  0 / 1 / 1. Read literally, the "fabricated specification" clause is met by all three models, the
  baseline included, so it could not separate them. **Nobody was eliminated; all three ran stage 2.**
- **Variance.** The 19 frozen stage-1 cases were executed again inside stage 2, so 21 turns have two
  independent runs per model. Verdicts agree on 21/21 (`gpt-4.1-mini`), 20/21 (`gpt-4o-mini`), 20/21
  (`gpt-4.1`). No further repeat was run: neither challenger was ahead in either stage, so a repeat
  could not change the decision.
- **Execution record:** one execution per case; 246/246 scored turns succeeded.

Infrastructure events (none is a model failure; details in `model_bakeoff.json`):

| When (UTC) | Event | Handling |
|---|---|---|
| 04:52 | greeting-only smoke failed for all three models: provider "no credits remaining" | owner added credits; smoke repeated and passed; not scored |
| 06:15 | `gpt-4.1`, stage 1, `rec-thin-wall`: provider rate limit, **30 000 tokens per minute** for `gpt-4.1` on this account | execution-failure rule: the case was executed again from its first turn in a new session; `gpt-4.1` runs were then paced (65 s / 30 s between cases, 60 s between turns) |
| 07:19–07:39 | local network outage on the operator machine during the baseline's stage 2 | the server's execution list showed the pending case had not been executed; the run resumed there; no case ran twice |

## 5. Correctness results

Manual verdict = product outcome of the turn (what the Core received after the guard, and the final
answer), one rubric for all models (`model_bakeoff_manual.json`). Automated = the unchanged 4E.2 layers.

| | `gpt-4.1-mini` | `gpt-4o-mini` | `gpt-4.1` |
|---|---|---|---|
| **Stage 2 (frozen 42): manual turns** | **44/44** | 42/44 | 41/44 |
| **Stage 2: manual cases** | **42/42** | 40/42 | 39/42 |
| Stage 2: automated turns | 40/44 | 38/44 | 29/44 |
| Stage 1 (subset): manual turns | **36/38** | 35/38 | 35/38 |
| Stage 1: manual cases | **27/29** | 26/29 | 26/29 |
| Stage 1: automated turns | 34/38 | 30/38 | 28/38 |
| All scored turns: manual | **80/82** | 77/82 | 76/82 |
| Regressions vs the accepted 4E.2 record (44/44) | 0 | 2 | 3 |
| Improvements vs 4E.2 at verdict level | — | none | none |

Failed turns (manual):

| Model | Turn | Why |
|---|---|---|
| `gpt-4.1-mini` | `bake-mt-budget-retention[1]` (s1) | budget carried correctly; "Эти модели поддерживают … VRR" also covers a model whose VRR is not listed |
| | `bake-mt-technology-retention[1]` (s1) | invented `hdmi_2_1` + budget (guard removed both); the answer still implies HDMI 2.1 for the listed models |
| `gpt-4o-mini` | `bake-budget-hdmi-explicit[0]` (s1) | "не имеют указанной функции": not listed presented as absent |
| | `bake-mt-carried-budget[1]` (s1) | six parallel `get_tv` calls, three refused by the cap; answer covers three of six models without saying so |
| | `bake-mt-feature-retention[1]` (s1) | no tool call for "А что лучше для игр?"; the carried HDMI 2.1 requirement is never applied |
| | `search-unavailable[0]` (s2) | count instead of a list: "9 моделей", none named |
| | `compare-exact[0]` (s2) | "Dolby Atmos: поддерживается только в …": not listed presented as absent |
| `gpt-4.1` | `rec-gaming[0]` (s1 **and** s2) | quality claims from its own knowledge: "отличной цветопередачей и быстрым откликом", "Яркая панель Neo QLED" |
| | `bake-mt-carried-budget[1]` (s1) | no tool call; "ALLM, Game Bar, VRR — … обычно есть в новых OLED Samsung" |
| | `bake-mt-feature-retention[1]` (s1) | dropped the HDMI 2.1 requirement stated one turn earlier |
| | `rec-impossible[0]` (s2) | no tool call; catalog statement from model knowledge |
| | `stats-cheapest[0]` (s2) | "есть ещё 9 более дешёвых моделей" — false; the 9 unavailable models all cost more |

The automated score of `gpt-4.1` is low mostly because its answers end with "…уточните, и я
подберу", which the scorer's question heuristic reads as a forbidden clarification. 14 of its 15
stage-2 automated failures are false positives (11 from that heuristic, 3 flags on a refusal or on a
statement about its own rules); each flag was checked by reading.

## 6. Tool / argument safety

Both stages together, 82 turns per model. "Invented" counts what the model proposed, before the guard.

| | `gpt-4.1-mini` | `gpt-4o-mini` | `gpt-4.1` |
|---|---|---|---|
| Tool calls / max per turn | 68 / 1 | 76 / **6** | 66 / 1 |
| Tool selection wrong (automated) | 0 | **5** | 2 |
| Invalid calls (`invalid_arguments`) | 0 | 0 | 0 |
| Unnecessary calls | 0 | **10** | 0 |
| Turns over the cap / calls refused by the cap | 0 / 0 | **1 / 3** | 0 / 0 |
| Invented hard constraints (model level) | **5** | 3 | 1 |
| — invented prices | 3 | 3 | 1 |
| — invented sizes | 1 | 0 | 0 |
| — invented required features | 1 | 0 | 0 |
| Invented hard constraints that reached the Core | **0** | **0** | **0** |
| Explicit constraints lost | 0 | 1 | 1 |
| Model ids not passed as written | 0 | 1 | 0 |
| Wrong filter values | 0 | 0 | 0 |
| Guard: unchanged / modified / report-only / not reached (cap) | 62 / 4 / 2 / 0 | 69 / 3 / 1 / 3 | 64 / 1 / 1 / 0 |

- The guard removed every invented hard constraint of every model; none reached the Core.
- `gpt-4.1-mini` invents the most (the known "А подешевле?" budget in both runs, "большой" → 65", "PS5"
  → HDMI 2.1 + budget in a follow-up), and depends on the guard the most.
- `gpt-4o-mini` sent the placeholder `max_price 1000000` that the prompt explicitly forbids
  ("Цена уже не важна."), and is the only model with cap refusals, redundant calls and wrong tools.
- `gpt-4.1` proposed one unstated value in 82 turns (`max_price 189990`, the price it had just shown).

## 7. Grounding

Manual counts, both stages. No model stated a fabricated model code or a fabricated price in any answer.

| | `gpt-4.1-mini` | `gpt-4o-mini` | `gpt-4.1` |
|---|---|---|---|
| Fabricated models | 0 | 0 | 0 |
| Fabricated prices | 0 | 0 | 0 |
| Unsupported feature / specification statements | 2 | 2 | 1 |
| Unsupported numeric claims | 0 | 0 | 1 |
| Unsupported quality claims / comparisons | 0 | 0 | **2** |
| Catalog statement without a tool call (answer level) | 0 | 0 | 1 |
| Wrong price basis / mislabelled price | 0 | 0 | 0 |
| Per-product claims checked automatically / issues confirmed by reading | 459 / 0 | 16 / 0 | 390 / 0 |
| Leaks (prompt, refs, internals) | 0 | 0 | 0 |

- The kinds differ: `gpt-4.1-mini` over-generalises a true list (group statement); `gpt-4o-mini` turns
  "not listed" into "does not have"; `gpt-4.1` adds facts the catalog does not contain (picture
  quality, response time, brightness, "обычно есть"), which the prompt forbids, and did so in
  `rec-gaming` in both runs.
- `gpt-4o-mini` has few automatically checkable claims because it puts each fact on its own line
  without the model code; its answers were read in full instead.
- One format deviation without a wrong fact: `gpt-4.1-mini`, `search-list-price` (stage 2), wrote
  "139 990 ₽ (сейчас 119 990 ₽)" — both prices correctly labelled, in the reverse of the prompt's order.

## 8. Multi-turn results

Stage 1, the seven multi-turn cases (16 turns), plus the same follow-up case in stage 2.

| Behaviour | `gpt-4.1-mini` | `gpt-4o-mini` | `gpt-4.1` |
|---|---|---|---|
| Budget retention, tool call in turn 1 (`carried-budget`) | **carried** `{OLED, 200000, gaming}` | not passed: 6 `get_tv` calls, 3 refused — **fail** | no tool call, model knowledge — **fail** |
| Budget retention, no tool call in turn 1 (`budget-retention`) | carried `120000`; answer has a VRR group error — **fail (grounding)** | carried | carried |
| Budget override 120 → 160 | applied | applied | applied |
| Budget release ("Цена уже не важна.") | no budget, asks | sent `1000000` (guard removed), asks | `{}`, asks |
| Size retention (65") | carried | carried | carried |
| Technology retention (OLED) + "для PS5" | OLED carried; invented HDMI 2.1 + budget, answer implies HDMI 2.1 — **fail** | carried, nothing invented | carried, nothing invented |
| Feature retention (HDMI 2.1) | **carried** | no tool call — **fail** | **dropped** — **fail** |
| Follow-up "Какой из них лучше для игр?" | `recommend_tvs` | 3 × `get_tv` (grounded) | `recommend_tvs` |
| Follow-up "А подешевле?" | invents `150000` (both runs), guard removes; true answer | invents `200000` (both runs), guard removes; true answer | `189990` / `≤ 65"`; best answer |
| **Multi-turn turns passed (stage 1)** | 14/16 | 14/16 | 14/16 |
| **Multi-turn cases passed (stage 1)** | 5/7 | 5/7 | 5/7 |

- Retention of the hard constraint itself (did the carried value reach the Core?): 6/6 for
  `gpt-4.1-mini`, 4/6 for `gpt-4o-mini`, 4/6 for `gpt-4.1`. The baseline's two failed turns are
  answer-level grounding errors, not memory errors.
- Conversation memory worked for every model; no cross-case leakage (one session per case).
- The 4E.2A provenance path was exercised live: 4 `report_only` decisions (first turn without a tool
  call), each keeping the user's carried constraint.

## 9. Answer quality

Bounded labels from reading all answers; six representative turns in the audit log: `rec-gaming`,
`compare-exact`, `rec-impossible`, `followup-oled65-spike[2]`, `bake-ps5-hdmi-explicit`,
`bake-mt-budget-retention[1]`.

| | `gpt-4.1-mini` | `gpt-4o-mini` | `gpt-4.1` |
|---|---|---|---|
| Length | concise–acceptable (sometimes 8 items) | **verbose**: one line per field for every product | acceptable |
| Recommendation reasoning | partial: lists the features | weak: "отлично подойдут" | **clear**: says who should take which and why |
| Differentiation between models | partial | partial | **useful** |
| Natural Russian | natural, plain | acceptable, templated; prices as "329990 ₽" / "22,990 ₽"; "была X" | natural |
| Clarification behaviour | appropriate | appropriate, thin | appropriate, proactive |

Example, "А подешевле?" after three 65" OLEDs (stage 1, shortened):

- `gpt-4.1-mini`: "В каталоге нет OLED телевизоров 65 дюймов для игр дешевле 150 000 ₽. Самый
  доступный вариант — … S85H … за 189 990 ₽."
- `gpt-4o-mini`: "…нет доступных OLED телевизоров размером 65 дюймов по цене ниже 200000 ₽, кроме
  уже упомянутой модели: … 189990 ₽."
- `gpt-4.1`: "…есть OLED-телевизоры Samsung дешевле, чем 65" S85H, но с меньшей диагональю: 42" —
  109 990 ₽, 48" — 119 990 ₽, 55" — 149 990 ₽ … Если для вас критична именно диагональ 65", дешевле в
  каталоге нет."

`gpt-4.1` is the best writer of the three. That advantage is style and usefulness on turns that were
already correct; it comes with the grounding failures of §5 and is not, by the gate's rule, a reason
to switch.

## 10. Latency

End-to-end latency of the Consultant sub-execution (same method for every model), seconds.

| | `gpt-4.1-mini` | `gpt-4o-mini` | `gpt-4.1` |
|---|---|---|---|
| Stage 2: median / p90 / p95 / max | 5.59 / 7.25 / 8.09 / 14.08 | 5.96 / 9.82 / 11.06 / 15.47 | 5.28 / 7.18 / 8.17 / 9.41 |
| Stage 1: median / p90 / p95 / max | 5.94 / 7.24 / 8.45 / 10.83 | 7.15 / 9.16 / 10.13 / 12.03 | 5.22 / 6.69 / 7.26 / 7.95 |
| All 82 turns: median / p95 | 5.66 / 8.45 | 6.31 / 10.79 | 5.22 / 7.79 |
| Stage 2 median: tool turns / no-tool turns | 5.95 / 3.61 | 6.69 / 3.37 | 5.70 / 3.47 |

- `gpt-4o-mini` is the slowest: its answers are longer and it makes more calls.
- `gpt-4.1` is about 0.4 s faster at the median than the baseline. Its runs were paced, so they
  started with an empty rate-limit window; the two mini models ran back to back.
- With 44 turns, p95 is the third-largest value; treat it as indicative.

## 11. Cost

n8n token estimates at list prices (the same method for every model).

| | `gpt-4.1-mini` | `gpt-4o-mini` | `gpt-4.1` |
|---|---|---|---|
| Stage 2 (44 turns): input / output tokens | 185 065 / 7 326 | 191 542 / 10 787 | 184 065 / 8 441 |
| Stage 2: cost | $0.086 | $0.035 | $0.436 |
| Stage 1 (38 turns): input / output tokens | 201 928 / 8 882 | 203 822 / 10 484 | 210 802 / 9 496 |
| Stage 1: cost | $0.095 | $0.037 | $0.498 |
| Both stages: cost | $0.18 | $0.07 | $0.93 |
| **Per user turn** (82 turns) | **$0.0022** | **$0.0009** | **$0.0114** |
| Relative to the baseline | 1× | 0.4× | 5.2× |

- The whole gate cost about $1.2 by this estimate.
- **The estimate is a lower bound.** n8n counts no completion tokens for a tool-call round and does not
  count tool schemas. The provider's own figure in the rate-limit message was about 1.3 times n8n's
  estimate for the same requests. The bias is the same for every model, so the ratios hold. Cached-input
  discounts are not modelled.

## 12. Production safety

| Check | Result |
|---|---|
| DB schema, catalog, `product_specs`, documents / chunks, embeddings | not touched; fingerprint and per-product lines identical before and after |
| Retrieval functions, ranking, Feature Registry, semantic guard | not touched; guard and tool-schema hashes equal in the container and the repository |
| Docker topology, Traefik, containers | `samsung-consultant:4e2a` healthy, same `StartedAt`, 0 restarts, no published port; n8n, postgres, redis, traefik not restarted |
| Consultant workflow | never modified; inactive; diff-identical to the repository after the gate |
| Other workflows | 16 workflows identical (id, name, active, `updatedAt`) to the snapshot taken when the runs started; the temporary driver was deleted; local driver backups removed |
| Model switching | only in the temporary driver's inline copy; production stayed on `gpt-4.1-mini` throughout |
| Ingestion, reindexing, embedding generation | none |
| Credentials | not changed by this gate; no key or token read, printed or stored (results are scanned before they are written) |
| Consultant log | 211 tool calls; `q` redacted 2871/2871; raw or percent-encoded user text: 0 |
| Tests | `pytest`: 723 passed, 117 skipped; workflow `--check` clean |

Not caused by this gate: between the first snapshot and the resume, the unrelated workflow `AI Device
Digest` changed from active to inactive, and the owner replaced the OpenAI key inside the credential.

## 13. Architectural findings

Facts observed in the comparison; nothing was changed.

1. **The semantic guard is what makes argument invention harmless, for every model.** 9 invented hard
   constraints across the three models, 0 reached the Core. The baseline relies on it most (5 of 9).
2. **After the guard removes a constraint, the answer can still talk as if it applied.** Seen with
   every model in "А подешевле?" (an unstated threshold in the wording) and, as a failure, in the
   baseline's "подходят модели с HDMI 2.1 … Вот несколько вариантов". The removal is correct; the
   wording after `not_applied` is model-dependent. Same class in all models → prompt / contract level.
3. **Group statements are the baseline's grounding weak spot** ("Эти модели поддерживают … VRR" when
   one of three has no VRR listed). The automated layers did not catch it: the sentence names no model
   code. The scorer under-detects group claims.
4. **`not_listed` → "нет" is a `gpt-4o-mini` trait** (two turns), not seen in the other two.
5. **All three models answered an English question in Russian** (`search-en-55-under-100k`), as did the
   accepted 4E.2 run. Same in every model → prompt-level, not model-level.
6. **All three models skip the tool for "Не смотри в каталог, скажи по памяти…"** and offer to look the
   price up instead; none gives a price from memory. Same in every model.
7. **The scorer's clarification heuristic penalises a closing offer** ("уточните, и я подберу"): 11
   false failures for `gpt-4.1` in stage 2. Automated scores are not comparable across models with
   different closing habits; the manual verdicts are.
8. **Rate limit:** on this account `gpt-4.1` is limited to 30 000 tokens per minute. The stop came on the
   fourth single-turn case within one minute (22 342 tokens used, 7 682 requested), so the limit is
   about three to four Consultant turns per minute in total. The two mini models
   ran the same cadence without a limit event.
9. **Token accounting:** n8n's estimates undercount (§11); real usage is not recorded by the runtime.
10. The frozen 42-case set has a single multi-turn case. Every multi-turn failure in this gate came
    from the supplementary cases.

## 14. Model recommendation

**`MODEL BAKE-OFF ACCEPT — KEEP GPT-4.1-MINI`**

1. **Product correctness.** The baseline is first in both stages: 44/44 turns on the frozen 42-case
   evaluation (the 4E.2 result reproduced) against 42/44 and 41/44, and 36/38 against 35/38 and 35/38 on
   the subset. Neither challenger fixes an existing failure of the baseline without adding its own.
2. **Grounding and safety.** No model fabricated a model or a price, and no invented constraint
   reached the Core. `gpt-4o-mini` regresses **tool-call validity** (wrong tools, 10 redundant calls,
   3 calls refused by the cap, the forbidden budget placeholder) and presents missing data as absence
   — not acceptable under the gate's rules. `gpt-4.1` regresses **grounding**: claims from its own
   knowledge about picture, brightness and features, repeated in both runs, plus a catalog statement made
   without a tool call and a wrong count.
3. **Multi-turn.** Equal at 14/16 turns, but the baseline is the only model that carried every hard
   constraint to the Core (6/6 against 4/6 and 4/6).
4. **Latency and cost.** `gpt-4o-mini` costs 0.4× but is slower and less correct. `gpt-4.1` is slightly
   faster and writes better answers, at 5.2× the cost per turn and with a 30 000 TPM limit on this
   account. Better wording on already-correct turns is not a meaningful product benefit under the
   gate's rule.

The result is not inconclusive, so `gpt-4o` is not proposed. The two baseline failures (findings 2 and
3) are prompt / contract-level and were deliberately not fixed here.

Nothing was deployed or merged. Production runs the accepted 4E.2A state with `gpt-4.1-mini`.
