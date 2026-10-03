# AI Catalog Consultant — Project Closure

Backend closed 2026-10-02; Telegram transport accepted 2026-10-03 (Phase 5A); released as AI Catalog Consultant on
2026-10-03 (Phase 4G). This document records the final state of the project: what is deployed, how it was
evaluated, what is known not to work, and how to roll back. The architecture is in [ARCHITECTURE.md](ARCHITECTURE.md).

The project was built and closed under the name *Samsung AI Consultant*. Phase 4G renamed it in the repository
(`projects/samsung-ai-consultant/` → `projects/ai-catalog-consultant/`) and nothing else: the deployed resources keep
their original identifiers (`samsung_rag`, `samsung-consultant`, the `Samsung — …` workflows), and the Samsung TV
catalog remains the reference dataset. The sections below dated 2026-10-02 are kept as recorded.

## Final status

**Completed portfolio MVP and reference implementation.** The system is frozen.

| | |
|---|---|
| Working MVP, live through Telegram | **accepted** by the project owner after live testing |
| Portfolio / reference implementation | **complete** |
| Strict experimental acceptance suite (Phase 4F.2) | not passed (`4F.2 HOLD`, 4 of 15); one known conversational limitation, documented and frozen |

The project is not claimed to be a production-ready retail assistant, an error-free autonomous recommendation
system, or a platform for arbitrary catalogs.

## Accepted production runtime

Verified read-only on 2026-10-02, after the closure cleanup. The Telegram transport added since is recorded
[below](#telegram-transport-phase-5a-2026-10-03).

| Item | Value |
|---|---|
| Consultant image | `samsung-consultant:4f3f`, id `sha256:f9e26b657efd…`, revision `afc8678` |
| Container | healthy, 0 restarts, internal network only, no published port, no reverse-proxy route, read-only root filesystem |
| Runtime code | `consultant/`, `indexing/`, `ingestion/`, `db/` and the workflow JSON at the closure commit are identical to revision `afc8678` |
| Tools | five: `search_tvs`, `get_tv`, `compare_tvs`, `recommend_tvs`, `get_catalog_stats`; contract `agent-result-v2`; at most 3 calls per user message |
| Semantic guard | `semantic-guard-4f3-v1` |
| Agent | n8n workflow `Samsung — AI Consultant` (`4d8mXFWGpS5P4t1L`), inactive; identical to `workflows/ai-consultant.json` |
| Model | `gpt-4.1-mini`, temperature 0; system prompt `consultant/prompts/agent_system_v3.md` |
| Retrieval | products selected and ordered by structured SQL, the Feature Registry and deterministic ranking; chunks supply catalog passages; no query embedding at runtime |
| n8n role | orchestration: the agent with its session memory, and the embedding job. Not a document builder, not a source of truth |

Canonical catalog state, from `deploy/consultant/catalog_fingerprint.py` run inside the container with the
Consultant's own read-only role:

| Item | Value |
|---|---|
| Catalog source | GalaxyStore Samsung TV catalog (`source = galaxystore`), a single commercial source |
| Products | 75, of which 66 available |
| Specification rows (`product_specs`) | 4 151 |
| Documents / chunks | 75 / 514, all 514 embedded |
| Embedding model / dimension | `text-embedding-3-small` / 1536 |
| PostgreSQL / pgvector | 16.15 / 0.8.6 |
| Newest product `updated_at` | 2026-09-28T18:03:49Z, the value recorded since Phase 4D: no product row has changed since |
| Products digest | `34f4d64c4ffd0a6af3c18dd68be06637`, equal to the Phase 4F.3 record |
| Database session | role `samsung_consultant`, `transaction_read_only = on` |

### Telegram transport (Phase 5A, 2026-10-03)

Verified read-only on 2026-10-03, when the transport branch was finalized into `main` (`9458270`).

| Item | Value |
|---|---|
| Channel workflow | n8n `TV Consultant — Telegram`, **active**; diff-identical to `workflows/telegram-transport.json` (9 nodes, 9 connections). Its id is kept out of the repository |
| Consultant workflow | `Samsung — AI Consultant` (`4d8mXFWGpS5P4t1L`), now **active**; diff-identical to `workflows/ai-consultant.json`, last updated 2026-10-01T13:57:50Z (unchanged since the closure) |
| Contract | Execute Workflow Trigger `{chatInput, sessionId}`; session `tg:<chat id>`, private chats only |
| Consultant service, model, prompt, guard, catalog | unchanged from the table above |
| Tests | 938 unit tests (45 new transport tests), 148 database tests, generator `--check` clean |
| Acceptance | live-tested and accepted by the project owner |

## Evaluation status

| Gate | Result |
|---|---|
| Strict product acceptance (Phase 4F.2) | **`4F.2 HOLD — PRODUCT ACCEPTANCE FAILED`**: 4 of 15 scenarios passed |
| MVP demo (Phase 4F.3) | **7 of 8** demo conversations pass; the three targeted acceptance defects are fixed |
| Hotfix 4F.3A (reject-and-retry) | failed live verification, reverted; production returned to `4f3f` |
| Telegram transport (Phase 5A) | accepted after live testing by the project owner |

The 7 of 8 is a demo result and does not replace the acceptance result. All evidence is kept as recorded:
retrieval evaluation (3D), agent evaluation (4D), query semantics and the guard (4E), the model bake-off, the
acceptance design and run (4F.1, 4F.2), the demo suite (4F.3) and the reverted hotfix (4F.3A). The index is
[evaluation/README.md](../evaluation/README.md).

What held in the final tested build: no invented model code, price or availability; hard constraints respected;
multi-turn constraint changes; unknown features reported as unknown; exact aggregates.

## Known limitation

**A vague or relative numeric request can be answered with a threshold the user never gave.**

After «а есть что-то подешевле?» or «не огромный» the model may invent a number (`max_price 50000`,
`max_screen_size_inches 55`) and put it into its tool call. The semantic guard removes it, so the catalog query is
not filtered by it and the returned products are right. The model still words its answer by that number («до
50 000 ₽», «до 55 дюймов»), as if the user had set it. Measured in Phase 4F.3: the number was removed in 32 of 32
sessions and still worded in most of them.

Rejecting the call instead of running it without the number (4F.3A) did not help: the model made no corrected
call and put the number to the user in a question. The remaining approach is a check of the answer after
generation, which is a new component and was not built.

Other limits, also documented in the README: the source catalog has no measured brightness; qualitative claims
need more evidence than structured catalog fields provide; a single source.

## Frozen scope

No further backend development is planned until one of these exists:

- a real client requirement;
- real user feedback;
- a production requirement;
- a requirement for a new data source.

## Rollback

No credential is needed to read this section, and none is recorded in the repository. Commands run as root on the
host, in the Consultant's Compose directory; the runbook is
[deploy/consultant/README.md](../deploy/consultant/README.md).

| Target | Procedure |
|---|---|
| Rebuild the accepted runtime | build the image from revision `afc8678` as in the runbook, load it, `docker compose up -d` |
| Roll back to the previous accepted build, `samsung-consultant:4e2a` (revision `8ea84f0`) | set `image: samsung-consultant:4e2a` (the retained `compose.yml.4e2a.bak` is that file) and `docker compose -f compose.yml up -d`. The workflow needs no restore: it carries the same prompt v3 and reads the tool list from the service |

`4e2a` is the build of the Phase 4F.2 acceptance run. It has no feature or series counts in `get_catalog_stats`,
reports no state for a required feature that was not applied, and its guard stands down after a slang number.
At closure the `4e2a` image was started as a temporary side container: healthy, authentication and the five tools
working; the container was then removed.

Only the `samsung-consultant` service is recreated by either procedure. n8n, PostgreSQL, Redis and Traefik
belong to another Compose project and are not touched.

## Closure cleanup

Performed 2026-10-02 after inspecting every artifact. The running container was not restarted (same `StartedAt`
before and after), and the catalog fingerprint is identical before and after.

| | Items |
|---|---|
| Removed images | `samsung-consultant:4f3`, `4f3a`, `4f3b`, `4f3c`, `4f3d`, `4f3e` (superseded Phase 4F.3 builds), `4f3g` (reverted 4F.3A hotfix) |
| Removed files | `compose.yml.4f3.bak`, `.4f3a.bak`, `.4f3b.bak`, `.4f3c.bak`, `.4f3d.bak`, `.4f3e.bak`, `.4f3g.bak`, and `.4f3f.bak` (identical to the current `compose.yml`) |
| Retained images | `4f3f` (current), `4e2a` (rollback); `4e2`, `4d2d`, `4d2b`, `4d2a` (earlier accepted gates, referenced by the runbook's older rollback steps) |
| Retained files | `compose.yml` (identical to the repository), `compose.yml.4e2a.bak` (rollback), `compose.yml.{4e2,4d2d,4d2b}.bak`, the env file, role scripts and probes |
| Workflow backups in the repository | all twelve pre-update backups of the Consultant workflow under `tools/n8n-tool/backups/samsung-ai-consultant/` are kept: they are the audit record of every workflow update, and four are rollback references in the runbook |
| Temporary evaluation drivers | none in n8n; the driver of the 4F.3A run was deleted after it, and its two local backups were removed |

Not changed: the n8n workflows (16, the Consultant workflow last updated 2026-10-01T13:57:50Z), database roles,
schema, catalog data, embeddings.

## Future optional work

**Optional.** None of these is an incomplete closure task.

- A web demo frontend (the Telegram channel exists since Phase 5A).
- Generalization to other catalogs: new extractors, section and feature vocabulary, tool schemas. Not implemented.
- Catalog enrichment from samsung.com.
- Production-grade answer validation: a check of the generated answer against the evidence.
- Better semantics for relative preferences («подешевле», «побольше»).
- Expanded evaluation: more scenarios, rates over repeated sessions.
- Observability and analytics.

## Verification at closure

| Check | Result |
|---|---|
| `python3 -m pytest -q -p no:cacheprovider` | 893 passed, 142 skipped (the database tests, run below) |
| `DOCKER_HOST=unix:///var/run/docker.sock bash tests/run_db_tests.sh` | 148 passed |
| `python3 -m consultant.n8n_workflow --check` | clean |
| `python3 -m evaluation.acceptance --check`, `acceptance_report --check`, `mvp_demo --check` | current |
| `python3 -m evaluation.guard_replay` | 35 of 35 and 22 of 22 |
| `python3 -m evaluation.semantic_eval` | 30 of 30 |
| Production smoke (`mcp_probe.js`) | health 200; 401 without or with a wrong token; five tools, closed schemas; a catalog call returns a product; the cap holds |
| Exposure | no published port, no host listener on 8765, `traefik.enable=false`, one internal network |
| Deployed workflow vs repository | no meaningful differences |
| Catalog fingerprint before and after cleanup | identical |
