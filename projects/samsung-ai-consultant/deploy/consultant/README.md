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
| `Dockerfile` (+ `Dockerfile.dockerignore`) | `python:3.12-slim-bookworm`, `psycopg2-binary` + `tiktoken` (encoding cached at build), `consultant/` + three `indexing/` modules, uid 10001, healthcheck `/healthz`. Build context: the project directory |
| `compose.yml` | Separate compose project `samsung-consultant`. It joins `n8n-compose_default` as an **external** network, so deploying it never recreates n8n, postgres, redis or traefik. No `ports`, no Traefik routers, read-only root FS, `cap_drop: ALL`, `no-new-privileges`, no volumes, no socket |
| `consultant.env.example` | Shape of the VPS-only `consultant.env` (mode 600, git-ignored) |
| `provision_env.py` | Generates the DB password and MCP token into `consultant.env`; emits the SCRAM verifier / token only into pipes |
| `create_consultant_role.sql`, `verify_consultant_role.sql` | Least-privilege read-only role `samsung_consultant` and its verification |
| `verify_readonly.py` | Write-denial check run inside the container with its own role |
| `mcp_probe.js` | Boundary probe run with Node inside the n8n container: health, 401 without or with a wrong token, authenticated `tools/list` + closed-schema check, one deterministic call |

## Runbook (as executed in Gate 4D.2A)

VPS directory: `/root/samsung-consultant/` (mode 700), containing `compose.yml`, `consultant.env`
and the scripts. Commands run as root on the VPS unless marked *local*.

```bash
# local: build from the deployed commit and ship the image (no registry, no repo on the VPS)
docker build -f deploy/consultant/Dockerfile --label org.opencontainers.image.revision=$(git rev-parse HEAD) \
  -t samsung-consultant:4d2a .
docker save samsung-consultant:4d2a | gzip | ssh n8n-vps 'gunzip | docker load'

python3 provision_env.py init-env consultant.env                       # secrets generated here, never printed
python3 provision_env.py psql-preamble consultant.env | cat - create_consultant_role.sql \
  | docker exec -i n8n-compose-postgres-1 psql -q -U n8n -d samsung_rag
docker exec -i n8n-compose-postgres-1 psql -U n8n -d samsung_rag < verify_consultant_role.sql
docker compose -f compose.yml up -d                                      # creates only samsung-consultant

# verification
python3 provision_env.py mcp-token consultant.env \
  | docker exec -i n8n-compose-n8n-1 node -e "$(cat mcp_probe.js)" http://samsung-consultant:8765
docker exec -i samsung-consultant python - < verify_readonly.py
ss -ltnup | grep 8765 || echo "no host listener";  docker port samsung-consultant
```

The n8n Header Auth credential (`Authorization: Bearer <token>`) is created through the n8n
public API. The token is piped from `provision_env.py mcp-token` and never displayed.

**Rollback:**
1. `docker compose -f compose.yml down` and `docker image rm samsung-consultant:4d2a`.
2. Delete the n8n credential and workflow.
3. As `n8n`, run `DROP OWNED BY samsung_consultant; DROP ROLE samsung_consultant;`.
4. `shred -u consultant.env`.
