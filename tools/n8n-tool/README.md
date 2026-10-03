# n8n Tool Automation

CLI tooling for treating n8n workflows as code: discover and export them
read-only (Phase 1), then validate, diff, and safely deploy them (Phase 2).
Built for the AI Catalog Consultant project (`projects/ai-catalog-consultant/`),
but not specific to it — reusable across any project in this repository.

```text
Cursor / Claude
      |
local workflow JSON
      |
   validation
      |
 remote discovery
      |
backup current remote
      |
      diff
      |
 explicit deploy
      |
  n8n REST API
      |
  read-back
      |
 verification
```

## Setup

```bash
cd ~/Documents/projects/12.n8n-tool_automation/tools/n8n-tool

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# edit .env and set N8N_BASE_URL / N8N_API_KEY
```

`.env` is git-ignored and is never read by anything other than this CLI on
your own machine.

Run the health check:

```bash
python -m n8n_tool doctor
```

## Commands

```bash
# Phase 1 — read-only
python -m n8n_tool doctor
python -m n8n_tool workflows list
python -m n8n_tool workflows find "Parsing"
python -m n8n_tool workflows get <workflow_id>
python -m n8n_tool workflows export <workflow_id>

# Phase 2 — validate, diff, and (carefully) write
python -m n8n_tool workflows validate <file>
python -m n8n_tool workflows diff <workflow_id> <file>
python -m n8n_tool workflows create <file> [--dry-run] [--yes] [--json]
python -m n8n_tool workflows update <workflow_id> <file> [--dry-run] [--yes] [--json] [--allow-protected]
python -m n8n_tool workflows deploy <file> [--dry-run] [--yes] [--json] [--allow-protected]
```

- **doctor** — verifies `N8N_BASE_URL` / `N8N_API_KEY` are set, checks
  connectivity, authentication, and that the workflows endpoint responds.
  Never prints the API key.
- **workflows list** — lists every workflow (ID, name, active, updatedAt),
  transparently following the n8n API's cursor-based pagination.
- **workflows find "<name>"** — looks up a workflow by name. Prefers an
  exact (case-insensitive) match; falls back to substring matches. If
  multiple workflows match, all candidates are printed — none is picked
  silently.
- **workflows get <id>** — fetches a workflow by ID and prints it as
  sanitized JSON, for diagnostics.
- **workflows export <id>** — fetches a workflow and writes it to
  `exports/<slug>/`:
  - `sanitized.json` — secrets stripped, safe to commit.
  - `raw.json` — as close to the raw API response as possible, for local
    analysis only (git-ignored). If the response appears to contain any
    sensitive field, `raw.json` is **not** written at all.
- **workflows validate <file>** — structural checks (valid JSON, unique
  node ids/names, connections reference real nodes, required node/workflow
  fields present) plus a secret scan. Add `--json` for machine-readable
  output.
- **workflows diff <id> <file>** — remote vs local, ignoring server noise
  (`updatedAt`, `createdAt`, `versionId`, `active`, ...), aligning nodes by
  name so reordering isn't reported as a rewrite.
- **workflows create <file>** — validates, scans for secrets, shows a plan,
  asks for confirmation (`[y/N]`, default No — use `--yes` to skip),
  creates the workflow, reads it back, and verifies it matches. Never sets
  `active`; new workflows are always created inactive by the API itself.
- **workflows update <id> <file>** — same pipeline, plus: fetches remote,
  computes a diff, and does nothing (`No changes detected. Nothing
  deployed.`) if there's no meaningful difference. If there is one, it
  takes a sanitized backup of the current remote state *before* writing,
  and refuses to update at all if that backup fails. After writing, it
  reads the workflow back and reports `Read-back verification: OK/FAILED`.
- **workflows deploy <file>** — convenience wrapper: reads a
  `<file base>.meta.json` sidecar for a `remoteWorkflowId`. None on file →
  create (and the id is written back to the sidecar on success). Present →
  update. The sidecar is local tooling state only; it is never sent to the
  n8n API.

### Safe deployment workflow

The recommended loop for developing a workflow as code:

```bash
python -m n8n_tool workflows validate workflows/foo.json

python -m n8n_tool workflows deploy workflows/foo.json --dry-run
# review: Validation, Secret scan, diff/plan, Action: CREATE or UPDATE

python -m n8n_tool workflows deploy workflows/foo.json
# interactive [y/N] confirmation, or add --yes for CI/automation
```

`--dry-run` never issues a POST/PUT — it's the mode to run before a real
deploy, and the only mode automation should default to.

## Phase 2 write-operation guarantees

- **No delete, ever.** There is no delete/bulk-delete code path anywhere in
  this repository.
- **No activation.** create/update never send `active`, and there is no
  activate/deactivate command. n8n's create/update endpoints treat `active`
  as read-only regardless; existing active state is preserved by the API
  itself when a workflow is updated.
- **No credential or execution writes.** Only `credentials.<type>.{id,name}`
  *references* are ever sent — never a secret value — because that's all
  n8n's own export/import already exposes.
- **Deployable payload allowlist.** The API's own OpenAPI spec
  (`GET /api/v1/openapi.yml` on the instance) declares
  `additionalProperties: false` on its workflow schemas, so
  `n8n_tool/normalizer.py` builds the request body from an explicit
  allowlist (`name`, `nodes`, `connections`, `settings`, `staticData`,
  `pinData`, plus `description` on update only) rather than forwarding a
  GET response — server fields (`id`, `active`, `createdAt`, `updatedAt`,
  `versionId`, `triggerCount`, `isArchived`, `meta`, `tags`, `shared`,
  `activeVersion`) are always dropped.
- **Secret scanning blocks deploy.** Every create/update run scans the
  local file for sensitive key names (`apiKey`, `token`, `password`,
  `secret`, `authorization`, `cookie`, ...) *and* secret-shaped values
  (OpenAI keys, AWS access key IDs, Google API keys, Slack tokens, JWT/
  Supabase-style service keys, PEM private keys, Bearer/Basic auth headers,
  credentials embedded in a URL) anywhere in the file, regardless of key
  name. A hit prints `DEPLOY BLOCKED` with the offending path — never the
  secret value itself — and stops before any API call.
- **Confirmation, not implicit writes.** Every real (non-dry-run) create or
  update shows a plan and asks `Proceed? [y/N]` with the default being No.
  `--yes` is required to skip it, and `--json` mode additionally *requires*
  `--yes` (there's no prompt to answer in scripted output).
- **Protected legacy workflow.** `n8n_tool/config.py` hardcodes the legacy
  `Parsing` workflow's ID as protected; `update`/`deploy` refuse to touch it
  unless `--allow-protected` is explicitly passed. This is a guard rail for
  Phase 2 integration testing, not a permanent restriction — lift it
  deliberately when a future phase intentionally reworks Parsing.
- **Read-back verification, no silent success.** After every write, the
  workflow is fetched again and diffed against what was intended. Mismatch
  → `Read-back verification: FAILED` plus the diff; for updates, the backup
  path is printed and no automatic rollback is attempted.

## API contract used

Confirmed directly against the instance's own published OpenAPI spec
(`GET /api/v1/openapi.yml`, served when the public API is enabled) rather
than guessed:

- Base path: `/api/v1`
- Auth: `X-N8N-API-KEY: <key>` header
- `GET /api/v1/workflows` — list workflows, paginated via an opaque
  `nextCursor` field (`{"data": [...], "nextCursor": "..." | null}`),
  requested again via `?cursor=<value>`.
- `GET /api/v1/workflows/{id}` — a single workflow, returned as a plain
  object.
- `POST /api/v1/workflows` — create (schema `workflowCreate`: requires
  `name`, `nodes`, `connections`, `settings`; also accepts `staticData`,
  `pinData`, `projectId`; `additionalProperties: false`).
- `PUT /api/v1/workflows/{id}` — update (schema `workflow`: same required
  fields, plus optional `description`; no `projectId`;
  `additionalProperties: false`). Per the spec: "If the workflow is
  published, the updated version will be automatically re-published" — i.e.
  update preserves whatever active/published state the workflow already
  had; this tool never sets it.
- Activation (`POST .../activate`, `.../deactivate`) and deletion
  (`DELETE /api/v1/workflows/{id}`) exist on the instance but are not
  called anywhere in this codebase.

The client (`n8n_tool/client.py`) also tolerates a bare-array response for
the list endpoint as a defensive fallback, without guessing at any other
shape. It exposes exactly five operations (list, get, create, update) plus
an internal `_request` helper — no generic
`request(method, endpoint, body)` is exposed to the CLI, so there is no way
to use this tool to issue an arbitrary write call.

## Security

- `.env` is listed in `.gitignore` and must never be committed.
- The API key is never printed, logged, or written into any exported,
  backed-up, or deployed file.
- `exports/**/raw.json` is git-ignored by default — local inspection only,
  and skipped entirely if the sanitizer detects a likely secret in the API
  response.
- `exports/**/sanitized.json` and every file under `backups/` have had any
  field whose *key* looks like a secret (API key, token, password, secret,
  authorization header, cookie, ...) replaced with `***REDACTED***` (see
  `n8n_tool/sanitizer.py`), while preserving workflow structure — nodes,
  connections, parameters, and credential *references* (id/name only).
- `workflows validate`/`create`/`update`/`deploy` additionally scan for
  secret-*shaped values* embedded anywhere in the file (see above) and
  block the operation if found.
- Phase 1 tooling exposes only HTTP GET. Phase 2 adds exactly two write
  operations (create, update) behind validation, secret scanning, backup,
  diff, and confirmation — see "Phase 2 write-operation guarantees" above
  for the full list of what is deliberately *not* implemented (delete,
  activate/deactivate, credentials, executions, arbitrary requests).

## Tests

```bash
pytest
```

Tests run entirely against mocked HTTP responses/fake clients — no network
access or live n8n instance is required.
