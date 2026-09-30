# Phase 4E.1 — Minimal Intent & Preference Semantics Spike

Status: **Gate 4E.1 evaluated (isolated spike, not integrated).** Recommendation: `4E.1 ACCEPT — READY
FOR MINIMAL RUNTIME INTEGRATION`, scoped as described in §9.

Gate 4E.2 (semantic guard) is recorded at the end of this document: offline `4E.2 HOLD — REVIEW REQUIRED`, then
live activation and the 42-case confirmation: `4E.2 ACCEPT — READY FOR GIT FINALIZATION` (§13).

Baseline: `main == origin/main == e333dadee058b1ce2ff33c8bcc6d2434298f5579` (Phase 4D accepted).
Branch: `feature/samsung-ai-consultant-intent-semantics`.

## 1. Question and starting point

The question: can a small typed layer `raw query -> intent / explicit filters / preferences` keep
stated requirements apart from implied wishes, without inventing hard constraints? And is it useful
enough to integrate?

The accepted 4D runtime already does a form of this. The n8n Agent picks one of five closed tools and
fills typed arguments: filters, `use_cases`, and `required_features` vs `preferred_features`. In the
4D.2E confirmation run, its arguments were correct in 31/33 scored turns. The two failures
(§17–§19 of the 4D record) are exactly the problem this phase targets:

| 4D.2E turn | User | Agent argument | Result |
|---|---|---|---|
| `rec-gaming` | "Посоветуй телевизор для PS5." | `required_features [hdmi_2_1]` (4D.2C: `+ hz_120`) | `no_match` |
| `followup-oled65-spike[2]` | "А подешевле?" | `max_price 150000` (4D.2C: 200000) | `no_match` |

The spike is therefore measured on two things: its own gold set, and whether it would have caught
these two inventions without flagging the correct turns.

## 2. Semantic contract (`consultant/query_semantics.py`)

```text
QuerySemantics
  intent       recommendation | comparison | product_question | catalog_question | null
  filters      panel_technology[]   resolution[] (4K/QHD/FHD/HD)
               screen_size_inches | min_screen_size_inches / max_screen_size_inches
               min_price / max_price   price_basis (list only when stated)
               min_refresh_rate_hz     availability (available / unavailable)
               models[]  (model codes or family tokens as written, 1-4)
  preferences  gaming | movies | sports | bright_room | audio | picture_quality
  notes        debug metadata, e.g. vague_price, vague_size, vague_brightness, vague_audio_power,
               negated_panel_technology:oled, optional_panel_technology:oled,
               feature_mention:hdmi_2_1, superlative:effective_price_asc, size_alternatives
```

- **Intents:** the four requested values. `null` means "not a catalog request" (greeting, general
  explanation). It is not a fifth intent. It is needed because 5/42 accepted 4D cases are `no_tool`,
  and forcing them into one of the four would attach filters to "что такое OLED".
- **Filters:** the field names and value schemas are taken by reference from the published
  `search_tvs` / `compare_tvs` schemas (`agent_tools.TOOL_SCHEMAS`). A test asserts they are the same
  objects. Semantics can therefore never hold a filter the tools would not accept, and the adapter is
  close to an identity mapping. The only addition beyond the brief's list is `price_basis`. Without
  it, "цена без скидки не больше 300 тысяч" would silently become a current-price bound. `category`
  (The Frame etc.) is deliberately left out.
- **No feature field:** VRR, HDMI 2.1, brightness and wattage cannot be represented at all. A named
  feature is recorded only as `feature_mention:<id>` and stays in the raw query.
- **No negative filters:** "только не OLED" and "OLED не принципиален" never produce a panel filter.
  They are recorded as `negated_…` / `optional_…` notes.
- **Validation:** `QuerySemantics.from_dict` is strict:
  - closed keys, closed enums, no coercion (the same `schema_errors` as the tool boundary);
  - plus the tool's range checks (exact size vs min/max, min ≤ max);
  - the parser's own output goes through it before it is returned.

## 3. Parser

`parse_query_semantics(query, vocab=None) -> SemanticParse` is deterministic, with no LLM and no I/O.

- **Filters** come only from spans the unchanged 4B extractor (`consultant.extract`) finds: model
  codes, families (with `vocab`), sizes, Hz, prices, panel/resolution keywords and availability.
  Bounds are classified in this module, because the 4B extractor reverses negated comparatives
  (finding F3). "55 или 65 дюймов" produces no size filter (`size_alternatives`). Around a price,
  "около" or no bound word produces no price filter.
- **Preferences** come from one closed stem lexicon per preference, for example `плойк|ps5|xbox|приставк|игр`
  → gaming, and `днём … блекл|светл… комнат|блик` → bright_room. A preference that is declared
  optional ("звук не важен") or negated ("не для игр") is dropped with a note. For audio, "без
  саундбара / без отдельной акустики" is *not* a negation of the audio need.
- **Intent** is a fixed rule order: ≥2 models → comparison; 1 model → product question; count
  wording or a superlative → catalog question; a preference or advice wording → recommendation;
  listing wording → catalog question; filters only → recommendation; otherwise `null`.
- **Failure contract:** it never raises. An empty or non-string query, >1000 characters, an internal
  exception, or output that fails validation all return `status="unavailable"`, `semantics=None`
  and an error class name. `unavailable` means "no semantic information, keep using the raw query".
  It is never "no filters" and never "no products match". A parse that succeeds with no filters is a
  distinct `ok` result (tested).

**Why not an LLM parser.** The brief allows structured LLM output "if that is the cleanest fit with
the existing stack". In this stack, Python never calls an LLM: there is no `openai` client and no key.
Every LLM call in 3D/4D ran inside n8n with its own credential, driven by a temporary workflow. This
gate forbids any n8n change. A live LLM parser could therefore not be built or evaluated here without
new approval. The deterministic result is itself a finding (F1): the hard-filter half of the problem
does not need an LLM.

## 4. Gold dataset (`evaluation/semantic_cases.json`)

It has 30 cases. The expectations were written from the query text alone **before the parser
existed**:
- sha256 `c17424131f56c4527a0f22a9475a116dd78f2a19f208a21dc7a753c059ee0e2a`, recorded at
  2026-09-30T15:22:30Z;
- the file is unchanged since then;
- 7 queries are copied from the 4D set (marked `4d:<id>`), and `agent_cases.json` is untouched.

| Group | Cases |
|---|---|
| explicit_filters | 5 |
| preferences | 5 |
| false_constraint_trap | 4 |
| mixed | 4 |
| comparison | 2 |
| product_question | 3 |
| catalog_question | 3 |
| colloquial | 3 |
| out_of_scope | 1 |

- **Trap letters:** 10 cases. A (PS5-like): 5; B (bright room): 2; C (powerful sound): 1;
  D (cheap): 1; E (large): 1.
- **`forbidden_filters`:** 13 cases list fields that must never appear. Independently of that list,
  every filter not in the gold is counted as invented.

Representative gold:

| Query | intent | filters | preferences |
|---|---|---|---|
| Посоветуй телевизор для PS5. | recommendation | — (forbidden: `min_refresh_rate_hz`) | gaming |
| Хочу OLED до 100 тысяч. | recommendation | panel OLED, max_price 100000 | — |
| Хочу телевизор с мощным звуком. | recommendation | — | audio |
| Посоветуй недорогой телевизор. | recommendation | — (forbidden: min/max price) | — |
| Хочу 65-дюймовый OLED до 150 тысяч для PS5 и кино, чтобы звук был хороший. | recommendation | size 65, panel OLED, max_price 150000 | gaming, movies, audio |
| S90H или S95H? | comparison | models S90H, S95H | — |

## 5. Results (`evaluation/results/semantic_eval_4e1.json`, `python -m evaluation.semantic_eval`)

**Run 1 (first execution, recorded as-is):** 29/30 cases. Intent 30/30, filters 30/30,
**0 invented hard filters**, all traps NO. One failure:
- `mix-oled-optional`, "Нужен телевизор для кино, OLED не принципиален.";
- the preference `movies` was missed;
- cause: the optionality check allowed one skipped word before "не принципиален", and that word could
  cross the comma, so the stance of OLED leaked onto "кино";
- class: *missed preference*, caused by a generic clause-scoping bug, not a wording gap.

**Fix (one round):** the skipped word may no longer cross punctuation. There were no other changes.

**Run 2 (final):**

| Metric | Result |
|---|---|
| Cases passed | **30/30** |
| Intent exact | 30/30 |
| Filters exact (case level) | 30/30. 26 expected items, 26 parsed, 0 missed, 0 wrong value |
| Preferences exact (case level) | 30/30. 17/17 labels found, 0 wrong. `col-daylight` / `trap-bright-screen` accepted extras: none used |
| `invented_hard_filter_count` | **0** |
| `invented_hard_filter_rate` | **0.0** (cases with ≥1 invented filter item / all cases) |
| Forbidden-field violations | 0 |
| Parse unavailable | 0 |

False-constraint safety (all trap cases, plus the downstream check that the shadow tool call carries
no `required_features` and resolves to `required = []`):

| Did the parser invent … | Answer |
|---|---|
| PS5 → 120 Hz | **NO** |
| PS5 → VRR | **NO** (no contract field; no required feature downstream) |
| PS5 → HDMI 2.1 | **NO** (same) |
| bright room → numeric brightness | **NO** |
| powerful audio → numeric wattage | **NO** |
| cheap → numeric price | **NO** |
| large → numeric screen size | **NO** |

**Unscored probes after the run** (not gold and not tuned; recorded as limitations):

| Probe | Output | Class |
|---|---|---|
| "Хочу OLED мне не важен размер" | OLED dropped (`optional_panel_technology`) | missed filter (safe direction) |
| "Нужен OLED, не важно какой диагонали" | OLED dropped | missed filter (safe direction) |
| "от 100 до 150 тысяч" | max 150000 only | missed filter: the 4B extractor ignores a bare "100" |
| "телевизор за 100 тысяч" | intent `null`, `unbounded_price` | wrong intent (fragment); no filter by design |
| "Хочу QLED или OLED до 200 тысяч" | `comparison`, panel [OLED, QLED] | wrong intent. The filter is correct (OR semantics) |
| "Какая яркость в нитах у QE65S95HAUXPY?" (4D `adv-brightness`) | product_question + picture_quality | extra soft preference; no filter |

Every limitation found fails toward *fewer* constraints. None produces a hard filter the user did not
state.

## 6. Shadow comparison (analysis only; nothing wired)

`evaluation/semantic_eval.shadow_tool_call` maps semantics to one existing tool call:
- `filters` become tool filter arguments, unchanged;
- preferences become `use_cases` only: gaming→gaming, movies→movies, audio→sound,
  bright_room→bright_room;
- it never emits `required_features` / `preferred_features`.

Every call is validated by the real `agent_tools.validate_arguments`. Recommend and search calls are
then resolved by the unchanged 4B `planning.resolve_plan`, using the fixture vocabulary. **30/30 calls
are valid.**

| Raw query | Parsed semantics | Tool call | Downstream (resolved by 4B planning) |
|---|---|---|---|
| Хочу 65-дюймовый OLED до 150 тысяч для PS5 и кино, чтобы звук был хороший. | rec; 65 / OLED / ≤150000; gaming, movies, audio | `recommend_tvs {panel OLED, size 65, max 150000, use_cases [gaming, movies, sound]}` | filters → SQL; required **[]**; preferred hz_120, vrr, freesync_premium, allm, game_bar, filmmaker_mode, dolby_atmos; numeric sound_power_w |
| Посоветуй телевизор для PS5. | rec; gaming | `recommend_tvs {use_cases [gaming]}` | required **[]**; the gaming profile as preferred. 4D.2E Agent: `required [hdmi_2_1]` → `no_match` |
| Нужен телевизор для PS5 со 120 Гц и HDMI 2.1, до 120 тысяч. | rec; ≥120 Hz, ≤120000; gaming; note hdmi_2_1 | `recommend_tvs {min_refresh_rate_hz 120, max_price 120000, use_cases [gaming]}` | 120 Hz is a hard filter because it was stated; HDMI 2.1 is not representable (F4) |
| Чтобы днём картинка не была блеклой. | rec; bright_room | `recommend_tvs {use_cases [bright_room]}` | preferred anti_glare; gap `brightness_not_in_catalog` kept |
| Посоветуй недорогой телевизор. | rec; —; note vague_price | `recommend_tvs {}` | CLARIFY (too vague). No budget is invented |
| Что лучше для PS5: S85H или S90H? | comparison; models; gaming | `compare_tvs {models [S85H, S90H]}` | gaming has no `compare_tvs` slot (unmapped) |
| Подходит ли QE65S95HAUXPY для игр? | product_question; model; gaming | `get_tv {model}` | gaming unmapped; the Agent chooses `attributes` |
| Сколько моделей дешевле 70 тысяч есть в наличии? | catalog_question; ≤70000, available | `search_tvs {…}` | should be `get_catalog_stats count`; the intent does not say which (F5) |
| Главное для меня — качество картинки. | rec; picture_quality | `recommend_tvs {}` | picture_quality has no ranking profile (F4) |

### Guard analysis on the accepted 4D.2E run

For each of the 44 recorded 4D.2E turns:
1. the user's messages in that case (all turns so far) were parsed;
2. every Agent argument that *constrains the candidate set* was checked against the stated filters.
   These are the filters, `required_features`, and `model(s)`. `use_cases`, `sort`, `limit`,
   `attributes` and the default `availability: available` do not exclude candidates and were not
   checked.

| | Result |
|---|---|
| Tool turns | 35 |
| Flagged | 3 |
| `rec-gaming[0]` | `required_features=hdmi_2_1`: **the known 4D.2E failure** |
| `followup-oled65-spike[2]` | `max_price=150000`: **the known 4D.2E failure** ("А подешевле?" gives `vague_price`, no number) |
| `compare-exact[0]` | `screen_size_inches=65`: benign. Both named codes are `QE65…` (implied by the model, not stated) |
| Missed manual failures | 0 of 2 |
| Use-case agreement with the Agent | 33/35. `rec-thin-wall`: the Agent's `thin_wall` is outside the 4E vocabulary (F2). Follow-up turn 3: the Agent carried `gaming` from context (4F) |

Filter extraction also matched the Agent's manually verified arguments on the 4D queries, which were
written in earlier phases. That includes the English query, list-price wording, availability,
families with a size, and unknown codes.

## 7. Compatibility with the Phase 4D architecture

| Semantics | Feeds | Existing component |
|---|---|---|
| `filters` | tool filter arguments (same names and schemas) → `agent_tools.constraints_from_args` → `QueryPlanDelta.constraints` | `Filters` + `catalog_repository.compile_filters`; availability / scope policy in `planning.resolve_plan` unchanged |
| `filters.models` | `get_tv.model` / `compare_tvs.models` → `model_ref` | `CatalogRepository.resolve_model_refs`; family-size clarification unchanged |
| `preferences` | `recommend_tvs.use_cases` (4 of 6 map) | `features.USE_CASES` profiles → `ResolvedPlan.preferred` / `numeric` → `ranking` (never `required`) |
| `intent` | advisory only: which tool family | the Agent's tool selection (it also distinguishes list / count / extreme, which the four intents do not) |

Integration can be **additive**: no change to retrieval, ranking, the Feature Registry, the tool
contracts or the evidence model is needed. The measured useful shape is a **grounding check at the
tool boundary**: semantics of the user's messages next to the Agent's arguments. It is not a
replacement of the Agent's argument extraction, because as a replacement it would lose `thin_wall`,
`compact`, the list/count/extreme choice, and feature attributes the Agent handles today.

## 8. Findings

- **F1 — Hard filters do not need an LLM.** The explicit filters were fully covered by
  deterministic extraction:
  - all 26 gold filter items;
  - every constraint the Agent used correctly in 4D.2E.

  The failure mode the brief worries about (an LLM turning "PS5" into 120 Hz / HDMI 2.1) is exactly
  what the live Agent did. A deterministic layer cannot do it by construction: numbers come only from
  spans in the text, and the contract has no feature field.
- **F2 — The requested preference vocabulary is narrower than 4D.** The 4D `use_cases` include
  `thin_wall` and `compact`, which 4E lacks. The accepted case `rec-thin-wall` depends on
  `thin_wall`.
- **F3 — Latent 4B extractor bug.** `consultant.extract` reads "не меньше / не дешевле N" as a
  maximum and "не больше N" as a minimum: `_MAX_WORDS` matches the suffix "меньше" / "дешевле"
  before the negation is considered. It has no runtime caller (tests only), so production is not
  affected. It was not changed in this gate. The semantic layer classifies bounds itself.
- **F4 — Two preferences and all named features have no downstream target.**
  - `sports` and `picture_quality` have no Feature Registry profile, so today they could only stay
    in the raw query.
  - An *explicit* feature requirement ("обязательно HDMI 2.1") is not representable in the minimal
    contract, although `recommend_tvs.required_features` exists.
- **F5 — Four intents do not determine the tool.** `catalog_question` covers `search_tvs` and both
  modes of `get_catalog_stats`. A comparison of technologies ("OLED или QLED") is ambiguous with a
  recommendation that has an OR filter. The intent is useful as a check, not as a router.

## 9. Gate recommendation

**`4E.1 ACCEPT — READY FOR MINIMAL RUNTIME INTEGRATION`**

- Every acceptance criterion is met:
  - typed contract;
  - validated parser with a safe failure contract;
  - 30 gold cases, with the 4D data untouched;
  - 0 invented hard filters and all traps NO;
  - 30/30 shadow calls valid at the real tool boundary;
  - existing tests green;
  - no production change.
- The layer is simple: one pure module, closed lexicons, and no new dependency.
- Its utility is demonstrated on real recorded traffic: it flags both known 4D.2E argument
  inventions, with one benign flag in 35 tool turns.

Scope conditions for 4E.2, taken from the findings, not added requirements:
- integrate as an additive grounding check next to the Agent's arguments, not as a replacement of
  its tool choice or `use_cases` (F2, F5);
- a flag should demote or report, never block, because parser misses fail toward fewer constraints
  (§5 probes);
- a flag on a size implied by a named full model code is benign (§6).

The deterministic parser was evaluated on a corpus written by the same author, so its robustness to
real user wording is not measured beyond the 44 4D.2E turns. An LLM-based variant was not evaluated
(§3).

## 10. Tests

| Suite | Before | After |
|---|---|---|
| Unit (`python3 -m pytest -q`) | 516 passed, 117 skipped | **612 passed, 117 skipped** (96 new in `tests/test_query_semantics.py`) |
| Workflow artifact (`python -m consultant.n8n_workflow --check`) | clean | clean |

The 117 skipped tests are the DB-backed suites. They need the disposable container
(`tests/run_db_tests.sh`) and were not run: no DB-facing code changed. No existing test was changed.

## 11. Production safety

- No database connection: the vocabulary comes from the committed test fixture.
- No n8n access, no LLM call, no embeddings.
- No container, image, compose or deployment change.
- No credential value was read. To find out whether Python could reach an LLM, only variable
  *names* were listed. There was no OpenAI, Anthropic or n8n variable in the environment. The project
  `.env` holds only `INDEXING_DATABASE_URL`; its value was not read.
- Runtime modules do not import the spike (tested). `agent_cases.json` and all earlier results files
  are unchanged.

---

# Gate 4E.2 — Semantic guard integration

Status: **the guard is implemented and integrated in the Python runtime, and the offline replay
passes. It is not activated in production and has no live 42-case confirmation.** Recommendation:
`4E.2 HOLD — REVIEW REQUIRED` (§12.9).

Baseline: `feature/samsung-ai-consultant-intent-semantics` at
`f9d18eb3c3a0553f17e4da3131cd89d24283858e` (Gate 4E.1), with `main == origin/main == e333dad`.

## 12.1 What the guard does (`consultant/semantic_guard.py`)

The Agent still selects the tool and fills its arguments. The guard only checks the Agent's **hard
constraints**, and it removes one only on positive evidence that the user never stated it.

| Argument | Removed when | Kept (examples) |
|---|---|---|
| `min_price`, `max_price` | The value appears as **no number** in any user message of the conversation. A number counts ×1, ×1 000 and ×1 000 000, so "150", "150к", "150 000" and "0,15 млн" all support 150000. | "до 150 тысяч", "under 100k", "от 100 до 150 тысяч" (the 4E.1 parser misses the minimum; the guard does not) |
| `price_basis` | Only together with the price bound it qualified | "цена без скидки не больше 300 тысяч" keeps `list` |
| `screen_size_inches`, `min_`/`max_screen_size_inches` | The value appears as no number. Digits inside a named model code count. | "65 дюймов", "не меньше 55", `QE65…` → 65, "S95H на 55 дюймов" |
| `min_refresh_rate_hz` | The value appears as no number | "120 Гц", "не меньше 120 Гц" |
| `required_features` (each) | The feature is **mentioned in no user message**. There is one closed pattern per Feature Registry id (14; a test asserts full coverage). | "обязательно HDMI 2.1", "нужен VRR", "120 Гц обязательно" (hz_120), "Dolby Atmos", "VESA" |
| `use_cases` | **Never removed.** If the guard removed something from a `recommend_tvs` call, use cases implied by the user's own words (gaming / movies / sound / bright_room) are merged in, so the soft ranking signal survives | `thin_wall`, `compact` and every other Agent use case pass through |
| panel, category, resolution, availability, models, sort, limit, stat, attributes, question, preferred features | **Never changed** (pass-through) | — |

- Spelled-out numbers ("бюджет сто тысяч") disable the numeric rules for that conversation, because
  the guard cannot read them.
- **Absence from `QuerySemantics.filters` is never used as evidence.** `QuerySemantics` supplies only
  the implied use cases for the merge.
- The guard only removes arguments or merges use cases, so it cannot add a price, size, refresh
  rate, feature, technology or resolution. A property check runs over every replayed call.
- **Intent is not used.** Tool routing stays with the Agent (4E.1 F5).
- `consultant.extract`'s reversed negated bounds (4E.1 F3) are not on the guard's decision path:
  the guard does not use bounds. It was left unchanged.

**Why the conversation, not one query.** In 4D.2E, follow-up turn 2 correctly carried OLED / 65"
from turn 1. A single-message guard would strip them. Known failure 2 ("А подешевле?" →
`max_price 150000`) is only distinguishable from a carried budget with the earlier messages.

**Fail-safe:** the guard never raises. In each of these cases the original arguments continue
unchanged into the 4D path:
- no conversation → `skipped`;
- original arguments the tool boundary rejects anyway → `skipped`;
- guarded arguments that fail `validate_arguments` / `constraints_from_args` → `fallback`;
- an internal error → `fallback`.

`ConsultantTools.call` also wraps the guard call itself.

## 12.2 Runtime flow and integration point

```text
user message ──(n8n)── Agent ── tools/call {name, arguments}
                                   │  /mcp?turn=<execution>&conv=<sessionId>&q=<message>   (q/conv: optional)
                                   ▼
mcp_server.do_POST ── parse_guard_params ── ConversationStore.record (derived evidence only, per turn)
                                   ▼
ConsultantTools.call:  per-turn cap → semantic_guard.guard_tool_arguments → run_tool
                                                                              │ validate_arguments (unchanged)
                                                                              ▼
                                  QueryPlanDelta → resolve_plan → route → execute → build_evidence (unchanged)
```

- **Integration point:** `ConsultantTools.call`, after the per-turn cap and before `run_tool`. The
  existing validation is not bypassed; guarded arguments are validated twice.
- **Agent-facing result:** when something was removed, the result gets
  `request.not_applied = {constraints: [{argument, value}], note}`. The note says the constraints were
  not stated by the user and not applied, and that results must not be presented as satisfying them.
  It is an additive field; the contract stays `agent-result-v2`. End users see nothing unless the
  Agent says it.
- **Log:** one `tool_call` line gains `guard`, `detail` and `[action, argument, value, reason]`.
  User text is never logged (tested). `q=` is redacted from the request log line (tested).
- **`ConversationStore`:**
  - keeps derived evidence only: numbers, feature ids and implied use cases, never text;
  - one entry per turn key, a window of 12 (≥ the Agent's memory window of 6);
  - the session TTL and a bound of 1000 conversations.
- **Transport modes:**
  - without `q` → no guard, and the exact 4D call shape (tested). This is **the deployed state**: the
    deployed workflow does not send `q`;
  - `q` without `conv` → report-only (`would_remove`), because earlier messages are unknown;
  - `q` with `conv` → the guard acts.

**Activation is not done.** It needs the workflow's MCP endpoint expression to become
`…/mcp?turn={{ $execution.id }}&conv={{ encodeURIComponent($json.sessionId) }}&q={{ encodeURIComponent($json.chatInput) }}`
in `consultant/n8n_workflow.py`. It also needs the regenerated workflow, a new Consultant image, and
the n8n update. All of these are n8n / Docker changes outside this gate. The generator was **not**
changed, so the committed workflow still equals the deployed one. Whether `$json.chatInput` resolves
inside the MCP Client Tool's endpoint expression is **unverified**. `$execution.id` was verified in
4D.2B-R.

## 12.3 Offline replay (`python -m evaluation.guard_replay` → `results/guard_replay_4e2.json`)

This is the frozen 4D.2E record (sha256 `fc47bd48…`). Each recorded Agent call was guarded with that
case's user messages so far. The expectation was registered in the replay module before the first
run: exactly the two known inventions change.

| Metric | Result |
|---|---|
| Recorded tool calls | 35 |
| Unchanged | 33 |
| Modified | 2, both expected. Judged correct: 2. False modifications: **0** |
| Validation failures of final arguments | 0 |
| Constraints added or altered by the guard | 0 |

| Turn | Agent (4D.2E) | Guard | Final args | Core outcome of identical args |
|---|---|---|---|---|
| `rec-gaming` "Посоветуй телевизор для PS5." | `{use_cases [gaming], required_features [hdmi_2_1]}` → `no_match` | removed `required_features=hdmi_2_1` (`required_feature_not_mentioned`) | `{use_cases [gaming]}` = the 4D gold | 4D.1 offline, same args: `ok`, `strong`, 8 ranked TVs |
| `followup-oled65-spike[2]` "А подешевле?" | `{OLED, 65, gaming, max_price 150000}` → `no_match` | removed `max_price=150000` (`unsupported_price_value`) | `{OLED, 65, gaming}` (carried OLED / 65 / gaming kept) | 4D.1 offline, same args (turn 2's call): `ok`, `strong`, the three 65" OLEDs |

Preserved (unchanged in the replay):
- `compare-exact`'s `screen_size_inches 65` from the named `QE65…` codes (the benign 4E.1 flag);
- `rec-thin-wall` `{55, use_cases [thin_wall]}`;
- follow-up turn 2's carried OLED / 65;
- every explicit price, size, list-price basis and availability.

**Supplementary cases** (`evaluation/guard_cases.json`, 22 cases written before the run, sha256
`e677d98b…`): **22/22**. They cover:
- brief tests A–I;
- "нужен VRR", "120 Гц обязательно" (`hz_120` + 120 Hz), "хороший игровой режим" → VRR removed;
- `compact` with and without an invented price;
- an invented size from "большой";
- a carried budget and a carried HDMI 2.1 (kept);
- a spelled budget (kept) and "100k" (kept);
- an invented OLED from "для PS5" (**kept**, documenting that the panel is out of the guard's scope).

## 12.4 Tests

| Suite | 4E.1 | 4E.2 |
|---|---|---|
| Unit (`cd projects/samsung-ai-consultant && python3 -m pytest -q`) | 612 passed, 117 skipped | **694 passed, 0 failed, 117 skipped** (82 new in `tests/test_semantic_guard.py`) |
| Disposable DB (`DOCKER_HOST=unix:///var/run/docker.sock bash tests/run_db_tests.sh`, local throwaway container) | not run | **123 passed** (4D.2E: 123) |
| 4E.1 semantic gold (`python -m evaluation.semantic_eval`) | 30/30, 0 invented | **30/30, 0 invented**; the results file regenerates byte-identical |
| Workflow artifact (`python -m consultant.n8n_workflow --check`) | clean | clean |

No existing test was changed. The 4D call shape without guard context is asserted by the unchanged
4D MCP tests plus a new one.

## 12.5 Final 42-case confirmation: **not run**

- **What a live run needs:**
  1. the n8n endpoint change (§12.2), without which the deployed runtime never sends `q`, so the
     guard stays inactive and a live run would only re-measure 4D;
  2. a new Consultant image recreated through the compose project;
  3. a temporary n8n driver workflow.

  The brief excludes n8n, Docker and compose changes, so none of this was done.
- **What is established offline:**
  - 33/35 recorded calls are unchanged. Their Core inputs are identical, so their tool results are
    identical: the Core is deterministic on a fixed catalog.
  - The two changed calls now equal calls whose Core outcome is recorded (`ok` / `strong`).
- **Not established:** the Agent's answers to the new results, and whether the Agent handles
  `request.not_applied` correctly. There is **no measured improvement or regression at the answer
  level**.

## 12.6 Production safety

- No database connection except the local disposable test container, which was removed on exit.
- Catalog, schema, n8n workflows and credentials untouched; no embeddings.
- No image build, deploy, restart or compose change. The deployed Consultant and workflow are
  unchanged.
- No credential read or printed. No other project touched.

## 12.7 Findings

- **G1 — Python cannot see the user's message.** Tool calls carry only `{name, arguments}` and the
  turn key. A live guard therefore depends on the workflow forwarding the message (§12.2). This is a
  transport gap, not a guard limitation.
- **G2 — The guard needs the conversation, not the query.** Carried constraints are legitimate
  (4D.2E follow-up turn 2), and known failure 2 is a relative follow-up. The server-side evidence is
  in memory, so after a Consultant restart the earlier messages of an ongoing conversation are
  unknown. A constraint stated before the restart and carried by the Agent would then be removed.
  The Agent is told through `not_applied` and can ask.
- **G3 — "Appears anywhere" is deliberately permissive.** A number from another context supports an
  Agent value. For example, after "Покажи OLED 65 дюймов" an invented `max_price 65000` would be kept
  (false keep, safe direction). Feature patterns are the opposite risk: a paraphrase outside the 14
  closed patterns would remove a stated requirement. The Agent would see `not_applied`. Neither was
  observed in the replay, but real-traffic coverage is unmeasured.
- **G4 — Some inventions stay out of scope.** Invented panel, resolution or availability pass
  through (supplementary case `X-no-invention-on-panel`). None occurred in 4D.2E.
- **G5 — One 4E.1 test checks less than its name says.** The 4E.1 isolation test
  (`test_runtime_does_not_use_the_spike`) is still true textually: `agent_tools` / `mcp_server` reach
  the parser only through `semantic_guard`, by a lazy import. By design it no longer proves they are
  isolated from it. The new test asserts that the pipeline modules (planning, router, retrieval,
  ranking, evidence, features, repository, payload) do not use the guard or the parser.

## 12.8 Stop conditions

None were triggered:
- the feature patterns are one per registry id, 14 in total, with no ontology;
- explicit and implied requirements are separated by an explicit mention in the text;
- integration is additive;
- the replay has no regression, and `thin_wall` / `compact` survive;
- no extra LLM call.

The guard is ~200 lines of pure functions.

## 12.9 Gate recommendation

**`4E.2 HOLD — REVIEW REQUIRED`**

- **Met offline:** every acceptance criterion that can be met without production changes:
  - both known inventions fixed;
  - 0 false modifications and 0 inventions by the guard;
  - explicit price / size / Hz / features, `thin_wall` / `compact` and model-code size preserved;
  - fail-safe tested;
  - existing suites green.
- **Not met:**
  - the mandatory live 42-case confirmation;
  - runtime activation.

  Both require the n8n endpoint change, an image deploy and a temporary driver, which this gate
  excludes.
- **For review:**
  - G2, restart behaviour;
  - G3, pattern coverage;
  - whether `$json.chatInput` resolves in the MCP endpoint expression.

  An approved activation gate would verify the last item first, then run the frozen 42-case
  confirmation.

---

# Gate 4E.2 — live activation and final 42-case confirmation

Status: **the guard is active on the live Agent → n8n → MCP → Python path, and the frozen 42-case
confirmation has been run.** Recommendation: `4E.2 ACCEPT — READY FOR GIT FINALIZATION` (§13.10).
The guard (`semantic_guard.py`, commit `d7de999`) was frozen; the only change is transport.

## 13.1 n8n data path (verified before any change)

- The deployed workflow `4d8mXFWGpS5P4t1L` was diff-identical to the committed artifact.
- In a recorded 4D.2E execution (sub-execution 811), the Agent's input item is exactly
  `{chatInput, sessionId}` from the evaluation trigger; the chat trigger emits the same fields.
- The Agent's sub-nodes (memory, `catalog`) receive only an `inputOverride` from the Agent. Their
  parameters resolve against the Agent's input item: the memory node's `={{ $json.sessionId }}`
  already worked in 4D (turn 2 of the follow-up case loaded turn 1).
- **Expressions:** message `$json.chatInput`; conversation `$json.sessionId`; turn `$execution.id`
  (unchanged since 4D.2B-R).
- The endpoint URL itself is not recorded in n8n run data, so resolution was proven on the server
  side (§13.4).

## 13.2 Workflow change (the only change to n8n behaviour)

```text
- =http://samsung-consultant:8765/mcp?turn={{ $execution.id }}
+ =http://samsung-consultant:8765/mcp?turn={{ $execution.id }}&conv={{ encodeURIComponent($json.sessionId) }}&q={{ encodeURIComponent($json.chatInput) }}
```

- Generated from `consultant/n8n_workflow.py` (commit `657e0e4`); `n8n-tool validate` OK.
- Deployed with `n8n-tool workflows update` at 18:40:31Z (backup
  `tools/n8n-tool/backups/samsung-ai-consultant/20260930T184031Z_4d8mXFWGpS5P4t1L.json`).
- The workflow is still inactive and diff-identical to the repository.
- The pinning unit test was updated to the new expression.

## 13.3 Privacy and logging

- **Application logs:**
  - `q` appears only as `q=[redacted]` in request lines, including requests that end in an error,
    because `log_error` goes through the same `log_message`;
  - the `tool_call` line holds argument names, values and reason codes, never text;
  - `log.exception` paths log tracebacks, not the request URL.
- **Checked on the live containers:**
  - the smoke container and production both had **0** occurrences of user text in their logs, raw
    or percent-encoded (Cyrillic words, "PS5", `%D0`/`%D1`);
  - after the run, 482/482 MCP POSTs carried `conv` + `q` and all were redacted.
- **`ConversationStore`:** keeps `MessageEvidence` only: numbers, feature ids, implied use cases, a
  flag. No text is kept (tested), and it is in memory only.
- **Outside the application's control:**
  - n8n itself stores full execution data, including the user message (`saveDataSuccessExecution:
    all`, unchanged since 4D);
  - if n8n's MCP client ever fails, its error message could contain the endpoint URL, `q`
    included, in n8n's execution data or container log. This was not observed.
  - The URL travels only inside the Docker network `n8n-compose_default`, over plain HTTP, as in 4D.
  - Docker's json-file log driver keeps only what the application writes.

## 13.4 Smoke test (before the production deploy)

A **temporary container** `samsung-consultant-4e2smoke` ran the new image with the production env
file, network and hardening, and no ports. It was removed afterwards. Production was not touched.

- **Boundary** (`mcp_probe.js`, from the n8n container): unchanged from 4D.
  - health 200; auth 401/401/200; five closed read-only tools; `attributes` 14/14;
  - `agent-result-v2`; `invalid_arguments`; cap `ok ok ok limit`; keyless call refused.
- **Guard** (`guard_probe.js`, new, from the n8n container):

| Probe | Result |
|---|---|
| PS5 args `{gaming, required [hdmi_2_1]}`, no `q` | 4D path: required kept → `no_match` |
| same, `q`="Посоветуй телевизор для PS5." + `conv` | `hdmi_2_1` removed, `not_applied` → `ok`, 8 products |
| same, `q`="…обязательно HDMI 2.1." | **kept** → `no_match` (hdmi_2_1 listed for 1/75, a display) |
| `q` without `conv` | `report_only`, arguments unchanged |
| "до 150 тысяч" + `max_price 150000` | kept |
| "для кино с хорошей картинкой" + `max_price 150000` | removed, `not_applied` |

- **Real Agent path:** a temporary smoke driver ran the committed workflow inline, pointed at the
  smoke host, with two turns. The Consultant's request lines carried `conv=e42smoke-0001` (the exact
  `sessionId`) and `q=[redacted]` on every request of both turns, and the guard ran on both calls.
  - Turn 2 ("Нужен OLED до 200 тысяч, обязательно с HDMI 2.1."): the Agent proposed
    `{OLED, max_price 200000, required [hdmi_2_1]}`, and the guard kept it (`unchanged`). That is only
    possible if the stored evidence holds an HDMI 2.1 mention and the number 200, which come only
    from turn 2's text. The right message reached Python.
  - Turn 1: the Agent proposed `{use_cases [gaming]}` (no invention), and the guard returned
    `unchanged`.

## 13.5 Build and deploy

| Item | Value |
|---|---|
| Image | `samsung-consultant:4e2`, revision label `657e0e4cb6b0609be54b5fd267354e8abd0ee7c0` |
| Image id | VPS `sha256:ca59d16f40eabaf9847254145cb0cfd6142f4eed0cb01f3db61b2ad4ba6f90b7` (local build `sha256:56a00a86…`; the stores differ, the revision label is the identity) |
| Tool-schema hash | `4645dac666b7ce5e`: container == local == 4D.2E |
| Deploy | `docker compose up -d` in `/root/samsung-consultant` at **2026-09-30T18:39:27Z**. Only the image tag changed (`compose.yml.4d2d.bak` kept). Container started 18:39:38Z, `healthy` |
| Isolation | no published port, no host listener on 8765, `traefik.enable=false`, `n8n-compose_default` only |
| Other containers | n8n, postgres, redis and traefik not restarted (identical `StartedAt`, 0 restarts) |
| Post-deploy | `mcp_probe.js` and `guard_probe.js` on production give results identical to the smoke container. `verify_readonly`: writes denied; known PUBLIC `TEMP` residual unchanged |
| Rollback | workflow backup `20260930T184031Z` **first**, then `4d2d`, because the old image logs full request lines (deploy README) |

## 13.6 Final 42-case confirmation (`evaluation/results/agent_eval_4e2.json`)

- **Frozen and identical to 4D.2E:**
  - dataset, prompt v3, scorer and live tooling (sha256 equal to the 4D.2E record);
  - `gpt-4.1-mini` at T=0, memory window 6, the five tools, ≤3 calls per turn;
  - Core and catalog: 75/66, md5 `7c1acf94…`, 4151 specs, 514 chunks, identical before and after.
- **Changed:** only the endpoint line and the image (the guard).
- **Procedure:** as in 4D.2E. One temporary driver was updated per case, with `sessionId`
  `e42-<case>`, and deleted afterwards. There was one run per case: **44/44 executions succeeded,
  0 retries, 0 infrastructure errors**.

| Metric | 4D.2E | **4E.2** |
|---|---|---|
| Turns / cases passed (manual) | 42/44 / 40/42 | **44/44 / 42/42** |
| Turns passed (automated) | 40/44 | 41/44 |
| Tool-needed accuracy | 44/44 | 41/41 headline + 3/3 follow-up turns handled (1 without a tool, see below) |
| Tool selection exact / exact-or-acceptable (headline) | — | 0.938 / 1.0 |
| Invented arguments (all categories) | 2 turns | **0** |
| Consultant calls / max per turn / blocked | 35 / 1 / 0 | 34 / 1 / 0 |
| `invalid_arguments` / execution errors | 0 / 0 | 0 / 0 |
| Clarification correctness | 2/2 | 1.0 (0 unnecessary) |
| Fabricated models / prices / features / unsupported numbers | 0/0/0/0 | **0/0/0/0** |
| Mislabelled prices / unsupported comparatives / lost gaps / leaks | 0/0/0/0 | **0/0/0/0** |
| Per-product claims checked / issues | — | 181 / **0** |
| **Guard:** calls seen / unchanged / modified / false modifications | — | **34 / 34 / 0 / 0** |
| Guard-induced validation failures | — | **0** |
| Latency median / p90 / max | 6.0 / 8.0 / 9.4 s | 6.0 / 9.0 / 11.3 s |
| Cost (n8n estimate) | ≈ $0.09 | ≈ $0.08 |

**Differences from 4D.2E**, each reviewed:

| Turn | 4D.2E | 4E.2 | Verdict |
|---|---|---|---|
| `rec-gaming` | `{gaming, required [hdmi_2_1]}` → `no_match` (fail) | `{gaming}` → `ok`, 3 grounded gaming TVs | **improved**; the Agent did not invent HDMI 2.1 (model variance, not a guard effect) |
| `followup-oled65-spike[2]` "А подешевле?" | invented `max_price 150000` → `no_match` (fail) | **no tool call**; answer from this session's turn 1/2 results: S85H 189 990 ₽, the cheapest 65" OLED, features grounded; no invented budget | **improved** at answer level. Automated fail: the label expects `recommend_tvs` (acceptable `search_tvs`) |
| `search-available-75` | `sort price_asc` | `limit 5` | pass (Agent argument variation) |
| `compare-exact` | `+ screen_size_inches 65` | without it | pass |
| `rec-sound` | `+ preferred [sound_power_w]` | `use_cases [sound]` only | pass |
| `no-tool-capabilities` | pass | automated flag on a capabilities description | pass (scorer false positive) |
| `stats-largest-oled-tie` | automated fail (category label) | the same | pass (as in 4D.2E) |
| `adv-invent-price` | automated fail (regex) | automated pass | pass |

- **Regressions:** none at answer level.
- **Explicit constraints:** the guard modified nothing, so every explicit constraint the Agent
  passed reached the Core unchanged. The analyzer's only "missing explicit" flags are
  `stats-cheapest-oled` and `stats-largest-oled-tie`: the Agent passed OLED as `category` instead
  of `panel_technology`, the same arguments as in 4D.2E, selecting the same 15 products. The guard
  does not touch either field. That covers budgets, size, minimum-size
  wording, list-price wording, availability, model codes, family + size, carried OLED / 65 / gaming
  in the follow-up, and `thin_wall`. The dataset has no `compact` and no explicit VRR / 120 Hz /
  HDMI 2.1 case: those are covered by the smoke probes, the smoke Agent turn (explicit HDMI 2.1 kept
  live) and the 22 supplementary replay cases.

## 13.7 The two known corrections at runtime

- **Correction capacity** is proven on the deployed runtime over the real transport: the production
  guard probe gives `hdmi_2_1` removed → `ok`, and an invented `max_price` removed.
- **In this run the Agent did not produce either invention** (0 invented arguments in 44 turns;
  also 0 in the two smoke turns). So there was nothing for the guard to correct, and both turns
  pass for that reason.
- **Therefore not observed live:**
  - a guard modification on an Agent-originated call;
  - how the Agent phrases an answer after `request.not_applied`.

## 13.8 Restart check (G2), on the smoke container, not production

| Step | Result |
|---|---|
| Turn 1 "Хочу OLED до 200 тысяч." + `{OLED, max_price 200000}` | unchanged |
| Turn 2 "А какой лучше для игр?", carried budget | unchanged (evidence from turn 1) |
| `docker restart`, then turn 3, same conversation and carried budget | **`max_price 200000` removed**, `not_applied` reported, 8 products instead of 6 |

The limitation is confirmed as documented. After a restart, a budget the user stated earlier is lost
for later turns. The Agent is told through `not_applied`; it is not silently kept, but the user's
constraint is not applied either. Production was not restarted for this check.

## 13.9 Tests and replays (after the transport change)

- `python3 -m pytest -q`: **694 passed, 0 failed, 117 skipped**.
- `DOCKER_HOST=unix:///var/run/docker.sock bash tests/run_db_tests.sh`: **123 passed**.
- `python -m consultant.n8n_workflow --check`: clean.
- Guard replay: 35 calls, 33 unchanged, 2 intended modifications, 0 false modifications, 0
  validation failures; 22/22 supplementary.
- Semantic gold: 30/30, 0 invented hard filters.

## 13.10 Cleanup, safety and recommendation

**Cleanup:**
- smoke container removed;
- smoke driver `jC4zA5VJbU8LLkdw` and evaluation driver `AuxjdJenkgfThOGb` deleted (the delete
  helper refuses any workflow not named "temporary, safe to delete");
- local temporary-driver backups removed;
- production `samsung-consultant:4e2` healthy, 0 restarts, no ports, no host listener;
- older images `4d2d`/`4d2b`/`4d2a` kept for rollback.

**Unrelated observation (not caused by this gate):** workflow `SuperRAG Agent` (`4psiovLFSioIEzD9`,
inactive, no executions) changed at 18:53:40Z, between the pre-run snapshot and cleanup.
- Its `OpenRouter Chat Model` node now has a credential reference; the 28 September export had none.
- This gate's only n8n calls in that window were a GET and a DELETE on the temporary driver.
- It was left untouched and needs the owner's confirmation.

**Safety:** DB schema, catalog, embeddings, credentials, n8n credentials, Traefik and other
containers were untouched. The only n8n changes were the Consultant workflow endpoint and the two
temporary drivers, both deleted.

**`4E.2 ACCEPT — READY FOR GIT FINALIZATION`.**
- **Every ACCEPT condition holds:**
  - the message reliably reaches the guard (every request of the run; proven by the smoke evidence
    chain);
  - no raw text is persisted or logged by the Consultant;
  - the smoke tests passed;
  - the full live 42-case confirmation was completed: 44/44 manual, no answer-level regression,
    0 false modifications, 0 guard-induced validation failures;
  - explicit constraints survived;
  - tests passed;
  - production is healthy;
  - temporary infrastructure was cleaned up.
- **The two known cases are correct at runtime,** with no hard constraint that the user never
  stated reaching the Core. In this run that happened because the Agent did not repeat the
  inventions. The guard's correction was demonstrated on the deployed runtime by the boundary probe,
  not by an Agent-originated call.
- **Open items:**
  - the Agent's handling of `not_applied` is unobserved live;
  - G2, restart loses evidence;
  - the unrelated SuperRAG change.

---

# Gate 4E.2A — restart-safe inherited constraints (root-cause fix only)

## What remembers previous turns

| Store | Where | Contents | Survives Consultant restart | Other lifecycle |
|---|---|---|---|---|
| Agent memory (`memoryBufferWindow`) | **n8n process**, global map `workflowId__sessionId` | all user and AI messages of the session; the Agent sees the last 6 exchanges | **yes** | lost on n8n restart; buffer cleared after 1 h idle; in the evaluation driver it lives in that `n8n execute` process |
| `ConversationStore` | **Consultant process** | derived evidence (numbers, feature ids, use cases), one entry per turn **that made a tool call** | **no** | 30 min idle TTL, 12 turns |

Nothing else persists: the Consultant has a read-only DB role, a read-only root FS and no volumes.

## Root cause

- The guard treated "absent from `ConversationStore`" as "not stated by the user". The store is not a
  complete record of what the Agent legitimately remembers:
  1. it is lost on a Consultant restart;
  2. it never sees turns without a tool call, because `q` only travels with MCP requests;
  3. it expires at 30 minutes, while n8n keeps history for 60.
- In the 4E.2 restart probe, the store was empty after `docker restart` and only held turn 3's
  evidence ("А какой лучше для игр?", no number). The Agent's carried `max_price 200000` was
  therefore judged unsupported and removed.

## Fix (guard rules unchanged)

- **n8n:** one node, "Prior turns" (Chat Memory Manager v1.1, `load`), reads **the same window
  memory** before the Agent runs.
  - It sits on a side branch of both triggers, placed above the Agent so execution order v1 runs it
    first; `onError: continueRegularOutput`.
  - The MCP endpoint adds `&h=<earlier user turns>`: the number of message groups holding a user
    message. `messagesCount` counts groups, and a turn with a tool call is stored as two, as seen
    live.
  - Unreadable output gives an empty `h`.
- **Consultant (`mcp_server.guard_context`):** the guard may act only if the store holds at least `h`
  earlier turns of this conversation. Otherwise, or with no or malformed `h`, it runs in the existing
  **report-only** mode: nothing is removed, and the decision is logged as `would_remove`.
- `semantic_guard.py` is unchanged.

## Verification

- **Unit tests (Consultant path, `guard_context` → store → `ConsultantTools.call` → guard):**
  - cases A–F from the brief (retention, override, removal, size, OLED, HDMI 2.1), each in three
    scenarios: same process, restart between turns, and turn 1 without a tool call. The Agent's
    turn-2 arguments reach the Core unchanged in all 18;
  - an expired store is report-only;
  - first-turn protections (PS5 → HDMI 2.1, invented budget, gaming → 120 Hz) are unchanged, and the
    fully observed follow-up invention is still removed.
- **Live, on a temporary smoke container (`samsung-consultant:4e2a`, revision of this fix) and
  temporary drivers:**

| Run | Result |
|---|---|
| Direct probes | first-turn corrections unchanged; after `docker restart`, the carried `max_price 200000` is **kept** (`report_only`, `would_remove`) |
| Agent path, "До 120 тысяч." → **restart** → "А что лучше для PS5?" | turn 1 made **no tool call**; turn 2 had `h=1` and the store 0 → `report_only`; **`max_price 120000` reached the Core** |
| Agent path, 3-turn follow-up without restart | `h` = 0, 1, 2; guard active. At "А подешевле?" the Agent invented **`max_price 200000`** and the guard **removed** it: the first correction observed on a live Agent call |

- **Other checks:** pytest 719 passed / 0 failed / 117 skipped; DB suite passed; workflow `--check`
  clean; replay and semantic gold unchanged.
- **Production is not changed:** `samsung-consultant:4e2` and the 4E.2 workflow are still deployed.
  The committed workflow now has 7 nodes (deployed: 6), and it and image `4e2a` await a deploy
  decision.
- Temporary drivers and the smoke container were removed.

## Limits (not addressed; outside the root cause)

- **Stale values after an override:** if the Agent kept sending an overridden or released value, the
  guard would keep it (it was stated earlier). The guard does not compute effective state. In the
  tests the Agent supplies the new state.
- **More report-only turns:** after any turn without a tool call, or after a restart, the guard is
  report-only for the rest of that conversation. This fails open, by design.
- **One more copy of the history in n8n:** the "Prior turns" node output holds the history in n8n
  execution data. The memory node's run data already holds the same messages. The Consultant still
  receives only a count, and keeps no text.
