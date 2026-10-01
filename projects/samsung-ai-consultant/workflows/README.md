# Samsung RAG n8n workflows (Phase 3B)

Two workflows, both created/deployed via `tools/n8n-tool` from the sanitized
JSON in this directory (never hand-edited only in the n8n UI):

- **`rag-indexing.json`** — `Samsung — RAG Indexing` (remote id
  `O0t1sOUHC7F5bD1B`) — embeds pending chunks and persists the vectors.
- **`rag-retrieval-smoke-test.json`** — `Samsung — RAG Retrieval Smoke Test`
  (remote id `AaOFPV2D982UkjoY`) — one-off query → embedding →
  `match_product_chunks()` check, not the 21-case benchmark.

Each has a `.meta.json` sidecar recording its `remoteWorkflowId` for
`n8n_tool workflows deploy` to resolve create-vs-update against.

## Why n8n doesn't call the Python `indexing` package directly

Investigated before writing any workflow, not assumed:

- `docker exec <n8n-container> which python3` → not found. The official
  `docker.n8n.io/n8nio/n8n` image ships Node.js only.
- `docker inspect <n8n-container>` → mounts are only `/files` (a local
  bind mount unrelated to this repo) and the n8n data volume
  (`/home/node/.n8n`). This git repository is not mounted into the
  container at all.

Making the Python package reachable from inside n8n would require adding
a Python runtime and/or mounting this repo into the container — a
Docker/compose change, explicitly out of scope for this phase (the task
brief requires stopping and reporting the reason first, not just doing
it). That's what's reported here instead: **the integration boundary is
the database, not a process call.**

`python -m indexing build --product-id <id>` (Phase 3A, unmodified) is
run as a separate, out-of-band step and writes `documents`/`chunks` rows
with `content`, `content_hash`, `metadata`, and `embedding = NULL`. Both
n8n workflows here start from "chunks that already exist but have no
embedding yet" — they never construct document/chunk text themselves.
The one JavaScript Code node in `rag-indexing.json`
(`Map embeddings to chunks`) only reshapes/validates the OpenAI API
response; it is not a reimplementation of the deterministic builder.

## `rag-indexing.json` — node-by-node

| Node | Responsibility |
|---|---|
| `When clicking 'Execute workflow'` | Manual trigger (n8n's Public API has no execute-workflow endpoint — see "Known limitations") |
| `Count pending chunks` | `SELECT count(*)::int` of chunks catalog-wide with `embedding IS NULL` — always returns exactly one row, so the next node can reliably branch even when the count is zero (see "Known limitations") |
| `Any pending chunks?` | IF branch on that count |
| `Get chunks pending embedding` (true branch) | Fetches all catalog-wide pending chunk rows (id, content), `ORDER BY c.id` for deterministic batch order |
| `Batch pending chunks` | `n8n-nodes-base.splitInBatches` (Loop Over Items), `batchSize: 50` — feeds pending chunks through the embed/persist chain 50 at a time; its `loop` output (index 1) drives `Build embeddings request`, and `Persist embedding` connects back into this node so batch *N*+1 is only requested once batch *N* is fully persisted. Its `done` output (index 0) goes to `Run summary` once all batches finish |
| `Build embeddings request` | Aggregates the *current batch's* (≤50) pending chunks into one item: parallel `chunk_id`/`content` arrays, for one batched OpenAI call per batch |
| `OpenAI: Create Embeddings` | `POST https://api.openai.com/v1/embeddings`, `model: text-embedding-3-small`, `input:` the current batch's content array. Auth: predefined credential type, `OpenAI account` — the key is injected by n8n and never appears in this workflow's JSON |
| `Map embeddings to chunks` | Pairs each returned embedding back to its `chunk_id` by response `index`; **throws** if any vector isn't exactly 1536-dimensional or if the model in the response doesn't match — refuses to persist a wrong-shape vector rather than silently writing one |
| `Persist embedding` | `UPDATE chunks SET embedding = '...'::vector, embedding_model = '...' WHERE id = ...`, once per chunk in the current batch, then loops back to `Batch pending chunks` for the next batch |
| `No pending chunks` (false branch) | Observable "nothing to do" status/message |
| `Run summary` | `executeOnce: true` — always runs exactly once after all batches finish (or immediately on the zero-pending branch), reporting `total_chunks` / `embedded_chunks` / `pending_chunks` per product catalog-wide |

**Phase 3C.E (full-catalog incremental indexing):** the original Phase 3B
`Indexing scope` node hardcoded `target_model_code = 'QE65S95HAUXPY'` as a
deliberate single-product safety gate for that phase's acceptance test.
Phase 3C.E removed that node and the corresponding `p.model_code = ...`
filters from `Count pending chunks` / `Get chunks pending embedding` /
`Run summary`, so indexing now runs catalog-wide on `embedding IS NULL`
alone, and added `Batch pending chunks` (bounded sequential batches of 50)
so a single run doesn't send the entire pending backlog to OpenAI in one
HTTP request. A partial failure mid-run leaves already-embedded chunks
committed; a rerun's `embedding IS NULL` selection naturally picks up only
what's left, with no separate resume state.

## `rag-retrieval-smoke-test.json` — node-by-node

`When clicking 'Execute workflow'` → `Query` (hardcoded Russian query
text + `match_count`) → `OpenAI: Embed query` (same credential/model,
single-string input) → `Extract query embedding` (validates 1536 dims,
same pattern as the indexing workflow) → `match_product_chunks`
(Postgres, calls the existing Phase 1 SQL function unmodified, `SELECT
... FROM match_product_chunks('...'::vector, match_count)`).

## Credential references used

Discovered via `GET /api/v1/credentials` (n8n's own OpenAPI spec
confirms this list endpoint never includes credential secret data —
only available to the instance owner/admin, `data` is `writeOnly`).
Never read/decrypted/printed/exported — only the `{id, name}` reference
is stored in the workflow JSON, exactly as n8n's `node.credentials` schema
expects.

| Credential | Type | id | Used by |
|---|---|---|---|
| `Samsung RAG PostgreSQL` | `postgres` | `iw2nbgnnNfRax7qn` | All Postgres nodes in both workflows |
| `OpenAI account` | `openAiApi` | `mcixQy0sFVXl7nU9` | Both `OpenAI: ...` HTTP Request nodes (predefined-credential-type auth) |

`Samsung RAG PostgreSQL` connects as the `samsung_indexing` Postgres role
(created by the project owner for this credential — not by this
codebase, and its password was never requested, read, or reset by this
work). See the top-level `README.md` "Database access" for that role's
grants (`SELECT` on `products`/`product_specs`, full CRUD on
`documents`/`chunks`).

## Known limitations / architectural findings discovered this phase

- **n8n's Public API cannot trigger a Manual Trigger execution.** Confirmed
  against the instance's own OpenAPI spec: `/executions` only supports
  list/get/retry/stop, no "run now." Both workflows here were executed by
  the project owner clicking "Execute workflow" in the n8n editor; this
  agent verified every run's outcome afterward via `GET /executions/{id}
  ?includeData=true` and direct (read-only, least-privilege) database
  queries — never by guessing.
- **A zero-item IF branch doesn't fire the "false" branch either — nothing
  downstream runs at all.** Discovered the hard way: the first version of
  `rag-indexing.json` had `Any pending chunks?` branch directly off a
  data-fetch query, and a genuinely-empty result set (0 rows) meant the IF
  node itself never executed, so neither branch — including the intended
  "No pending chunks" observability node — ever ran. Fixed by inserting
  `Count pending chunks` (a `SELECT count(*)`, which *always* returns
  exactly one row) ahead of the IF node, so there is always at least one
  item for it to evaluate. Verified via three real executions: #486 (7
  pending → real embeddings), #487 (pre-fix, 0 pending → silently
  produced no observable output, confirming the bug), #489 (post-fix, 0
  pending → `No pending chunks` → `Run summary` both correctly fired).
- **Postgres `count(*)` returns `bigint`, which n8n's driver surfaces as a
  string** — broke the IF node's strict-typed numeric comparison
  (`NodeOperationError: Wrong type: '0' is a string but was expecting a
  number`) on the very first post-fix test run. Fixed with an explicit
  `::int` cast in the `Count pending chunks` query rather than loosening
  the IF node's type validation.
- **Phase 3A's `replace_chunks` (delete-then-reinsert) was not
  embedding-aware — fixed in Phase 3B.1.** Once Phase 3B gave chunks real
  embeddings, re-running `python -m indexing build` on an unchanged
  product was silently discarding them (new row `id`s, `embedding` reset
  to `NULL`) on every rebuild, directly defeating Phase 3B's own stated
  incremental-indexing goal. `indexing/repository.py`'s `sync_chunks`
  replaces that: it matches chunks by logical section
  (`metadata->>'section'`) and only clears `embedding`/`embedding_model`
  when `content_hash` actually differs — see
  [`docs/adr/003-embedding-aware-chunk-sync.md`](../docs/adr/003-embedding-aware-chunk-sync.md)
  and `indexing/README.md` "Persistence / re-indexing contract" for the
  full fix. Verified against the real production `QE65S95HAUXPY` row: a
  live rebuild preserved all 7 chunks' embeddings byte-for-byte (SHA-256
  fingerprint match on Postgres's own vector text rendering), and a
  follow-up run of this workflow correctly found zero pending chunks and
  never executed the `OpenAI: Create Embeddings` node at all (confirmed
  via `GET /executions/{id}?includeData=true`, not assumed from timing).
  This workflow's own contract and nodes were **not changed** by that fix
  — `embedding IS NULL` was always the correct thing for `Count/Get
  chunks pending embedding` to check; it was the Python side that needed
  to stop clearing that column unnecessarily.
- **No webhook/API trigger was added.** Considered (to let this agent
  trigger executions programmatically) and deliberately not done — it
  would require activating the workflow on the shared, production n8n
  instance, which is a bigger and more persistent state change than
  asking for one manual click; see the conversation's own review of this
  tradeoff. Manual Trigger only, workflow left **inactive**.

## Evaluation-only workflows (temporary, NOT production architecture)

`evaluation-query-embeddings.json` (committed) was used for Phase 3D (see
[../docs/phase-3d-retrieval-evaluation.md](../docs/phase-3d-retrieval-evaluation.md)). It is an inactive,
manual-trigger workflow with no database node. The later Phase 3D chat-completion workflows
(`evaluation-consultant-spike.json`, `evaluation-fullcatalog-spike.json`) are generated, git-ignored
artifacts: rebuild them with `python -m evaluation.make_llm_workflow [fullcatalog]` from the committed
`evaluation/results/*_contexts.json`. All three definitions were deployed in turn to the single remote
workflow `CF5wtjEB9MFR5f0N`, which now holds the full-catalog definition; delete it in the n8n UI when no
longer needed. They exist for reproducibility only and must not be treated as a design for the Consultant.

## `ai-consultant.json` — `Samsung — AI Consultant` (Phase 4D, deployed inactive)

Generated by `python -m consultant.n8n_workflow` from `consultant/prompts/agent_system_v3.md` (since Gate 4D.2D; v2 before) and
`consultant.agent_tools.TOOL_SCHEMAS` (never hand-edit; `--check` / the unit tests fail when stale).
AI Agent (v3.1, `gpt-4.1-mini`, T=0, `maxIterations` 4) with the existing OpenAI
credential (reference only; n8n reports its display name as `OpenAI account samsung-ai` since 2026-10-01, same id), in-process window memory, and one MCP Client Tool (`catalog`) that
reaches the Python tools over MCP Streamable HTTP. Entry points: editor chat (`public: false`) and
an Execute Workflow Trigger for a controlled evaluation driver. No webhook, no Telegram.

**Deployed in Gate 4D.2A, inactive:** remote id `4d8mXFWGpS5P4t1L` (`ai-consultant.meta.json`). The MCP
endpoint is the internal Docker service `http://samsung-consultant:8765/mcp` (no published port; see
[../deploy/consultant/README.md](../deploy/consultant/README.md)). Since Gate 4D.2B-R the node calls
`…/mcp?turn={{ $execution.id }}`, so the server's 3-call cap counts per user message, not per MCP
session. Credential references: `OpenAI account samsung-ai`
and the Header Auth credential `Samsung Consultant MCP` (`9Ak6Ely4Wbp63RUn`), by id/name only.
