# Samsung Consultant runtime — internal Docker service (Phase 4D.2A)

The Phase 4D MCP server (`python -m consultant.mcp_server --container`) is deployed as an **internal
Docker service** on the existing `n8n-compose_default` network. There is **no published port**
([ADR 004](../../docs/adr/004-agent-runtime-and-tool-boundary.md) explains why a host bind to the
bridge gateway was rejected).

```text
n8n ──MCP + Bearer token──> samsung-consultant:8765 ──read-only role──> postgres:5432/samsung_rag
                 (all inside n8n-compose_default; nothing on a host interface)
```

| File | Purpose |
|---|---|
| `Dockerfile` (+ `Dockerfile.dockerignore`) | `python:3.12-slim-bookworm`, `psycopg2-binary` + `tiktoken` (encoding cached at build), `consultant/` + three `indexing/` modules, uid 10001, healthcheck `/healthz`, entrypoint with `--require-turn-key` (4D.2B-R). Build context: the project directory |
| `compose.yml` | Separate compose project `samsung-consultant`. It joins `n8n-compose_default` as an **external** network, so deploying it never recreates n8n, postgres, redis or traefik. No `ports`, no Traefik routers, read-only root FS, `cap_drop: ALL`, `no-new-privileges`, no volumes, no socket |
| `consultant.env.example` | Shape of the VPS-only `consultant.env` (mode 600, git-ignored) |
| `provision_env.py` | Generates the DB password and MCP token into `consultant.env`; emits the SCRAM verifier / token only into pipes |
| `create_consultant_role.sql`, `verify_consultant_role.sql` | Least-privilege read-only role `samsung_consultant` and its verification |
| `verify_readonly.py` | Write-denial check run inside the container with its own role |
| `guard_probe.js` | Gate 4E.2 semantic-guard probe run with Node inside the n8n container: guard decisions over the real MCP boundary (PS5 without/with message, explicit HDMI 2.1, report-only without `conv`, explicit vs invented budget) and the two halves of the in-memory restart check |
| `mvp_probe.js` | Phase 4F.3 probe run with Node inside the n8n container: an unapplied required feature is reported as `not_listed` per product (and nothing else is added to the result), an invented number after a slang budget is removed and a stated one kept, feature / series counts with their scope and listed values |
| `mcp_probe.js` | Boundary probe run with Node inside the n8n container: health, 401 without or with a wrong token, authenticated `tools/list` + closed-schema check, one deterministic call; since 4D.2B-R also the per-turn cap across four fresh sessions (`ok ok ok tool_call_limit_reached`) and refusal of a call without `?turn=`; since 4D.2D also the result contract and the `attributes` limit |

## Runbook (as executed in Gate 4D.2A)

VPS directory: `/root/samsung-consultant/` (mode 700), containing `compose.yml`, `consultant.env`
and the scripts. Commands run as root on the VPS unless marked *local*.

```bash
# local: build from the deployed commit and ship the image (no registry, no repo on the VPS)
docker build -f deploy/consultant/Dockerfile --label org.opencontainers.image.revision=$(git rev-parse HEAD) \
  -t samsung-consultant:4f3f .                                       # 4F.3 (4E.2A: 4e2a / 8ea84f0; 4E.2: 4e2 / 657e0e4)
docker save samsung-consultant:4f3f -o consultant.tar && gzip consultant.tar      # a piped save|ssh stalled in 4D.2B-R
scp consultant.tar.gz n8n-vps:/root/ && ssh n8n-vps 'gunzip -c /root/consultant.tar.gz | docker load && rm /root/consultant.tar.gz'

python3 provision_env.py init-env consultant.env                       # secrets generated here, never printed
python3 provision_env.py psql-preamble consultant.env | cat - create_consultant_role.sql \
  | docker exec -i n8n-compose-postgres-1 psql -q -U n8n -d samsung_rag
docker exec -i n8n-compose-postgres-1 psql -U n8n -d samsung_rag < verify_consultant_role.sql
docker compose -f compose.yml up -d                                      # creates only samsung-consultant

# verification
python3 provision_env.py mcp-token consultant.env \
  | docker exec -i n8n-compose-n8n-1 node -e "$(cat mcp_probe.js)" http://samsung-consultant:8765
docker exec -i samsung-consultant python - < verify_readonly.py
python3 provision_env.py mcp-token consultant.env \
  | docker exec -i n8n-compose-n8n-1 node -e "$(cat guard_probe.js)" http://samsung-consultant:8765   # 4E.2 guard
ss -ltnup | grep 8765 || echo "no host listener";  docker port samsung-consultant
```

The n8n Header Auth credential (`Authorization: Bearer <token>`) is created through the n8n
public API. The token is piped from `provision_env.py mcp-token` and never displayed.

**Rollback of Gate 4E.2A:** set `image: samsung-consultant:4e2` (`compose.yml.4e2.bak`) and `up -d`, and restore
the workflow backup `tools/n8n-tool/backups/samsung-ai-consultant/20260930T194205Z_4d8mXFWGpS5P4t1L.json`
(6 nodes, no `h`). Either order is privacy-safe: both images redact `q`. Note that `4e2` ignores `h` and has the
restart/no-tool-turn provenance defect.

**Rollback of Gate 4E.2 (order matters):** first restore the Consultant workflow backup
`tools/n8n-tool/backups/samsung-ai-consultant/20260930T184031Z_4d8mXFWGpS5P4t1L.json` (endpoint without `conv`/`q`),
**then** set `image: samsung-consultant:4d2d` (`compose.yml.4d2d.bak` on the VPS) and `up -d`. The `4d2d` server logs the
full request line, so it must never receive `q` (the user's message).

**Rollback:**
1. `docker compose -f compose.yml down` and `docker image rm samsung-consultant:4d2d`. To go back one gate, set
   `image: samsung-consultant:4d2b` (`compose.yml.4d2b.bak` on the VPS) and `up -d`, **and** restore the
   Consultant workflow backup `tools/n8n-tool/backups/samsung-ai-consultant/20260930T123254Z_4d8mXFWGpS5P4t1L.json`
   (prompt v2): prompt v3 expects the `agent-result-v2` price fields. `4d2a` lacks the per-turn key.
2. Delete the n8n credential and workflow.
3. As `n8n`, run `DROP OWNED BY samsung_consultant; DROP ROLE samsung_consultant;`.
4. `shred -u consultant.env`.
