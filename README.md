# n8n Tool Automation — Phase 1

Read-only CLI tooling for connecting to an n8n instance via its public REST
API, discovering workflows, and exporting them to Git-safe JSON.

This is **Phase 1** of the Samsung TV AI Consultant v2 rebuild. Scope is
strictly limited to safe, read-only operations against n8n — see
[Security](#security) below. No workflow is created, updated, activated,
deactivated or deleted by anything in this repository.

## Setup

```bash
cd ~/Documents/projects/12.n8n-tool_automation

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
python -m n8n_tool doctor
python -m n8n_tool workflows list
python -m n8n_tool workflows find "Parsing"
python -m n8n_tool workflows get <workflow_id>
python -m n8n_tool workflows export <workflow_id>
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

## API contract used

This tool targets n8n's official public REST API:

- Base path: `/api/v1`
- Auth: `X-N8N-API-KEY: <key>` header (see n8n docs:
  https://docs.n8n.io/api/authentication/)
- `GET /api/v1/workflows` — list workflows, paginated via an opaque
  `nextCursor` field (`{"data": [...], "nextCursor": "..." | null}`),
  requested again via `?cursor=<value>`.
- `GET /api/v1/workflows/{id}` — a single workflow, returned as a plain
  object.

The client (`n8n_tool/client.py`) also tolerates a bare-array response for
the list endpoint as a defensive fallback, in case of API version
differences, without guessing at any other shape.

## Security

- `.env` is listed in `.gitignore` and must never be committed.
- The API key is never printed, logged, or written into any exported file.
  `doctor` only ever shows the base URL and pass/fail status.
- `exports/**/raw.json` is git-ignored by default — it exists only for
  local, ad-hoc inspection and is skipped entirely if the sanitizer detects
  anything that looks like a secret in the API response.
- `exports/**/sanitized.json` has had every field whose key looks like an
  API key, token, password, secret, authorization header, or cookie
  replaced with `***REDACTED***` (see `n8n_tool/sanitizer.py`), while
  preserving workflow structure — nodes, connections, parameters, and
  credential *references* (id/name only, never the secret itself).
- Phase 1 tooling exposes only HTTP GET. There is no code path in this
  repository capable of creating, updating, activating, deactivating, or
  deleting a workflow, credential, or execution in n8n.

## Tests

```bash
pytest
```

Tests run entirely against mocked HTTP responses/fake clients — no network
access or live n8n instance is required.
