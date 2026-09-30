# Phase 4E.1 — Minimal Intent & Preference Semantics Spike

Status: **Gate 4E.1 evaluated (isolated spike, not integrated).** Recommendation: `4E.1 ACCEPT — READY
FOR MINIMAL RUNTIME INTEGRATION`, scoped as described in §9.

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
