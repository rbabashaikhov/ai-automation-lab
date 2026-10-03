# ADR 004: Agent runtime ownership and the Agent → Python tool boundary (Phase 4D)

## Status

Accepted for Gate 4D.1 (offline). The live boundary (Gate 4D.2) is **blocked pending an
infrastructure approval**: see "Consequences". This ADR amends Phase 4A. It does not rewrite it:
[`PHASE_4A_AI_CONSULTANT_ARCHITECTURE.md`](../PHASE_4A_AI_CONSULTANT_ARCHITECTURE.md) stays as
written.

## Context

Phase 4A proposed a deterministic Python pipeline with two bounded LLM calls: a structured
query-understanding call (G1), deterministic routing, then an answer call (G2). It explicitly
rejected LLM tool calling (4A §20) because routing would become non-deterministic and the LLM
could choose similarity ranking for "cheapest".

Phase 4B/4C then built the deterministic domain layer: typed plans, allowlisted SQL, the Feature
Registry, deterministic ranking, shortlist composition, and evidence with gaps. For Phase 4D, the
product owner decided that the conversational runtime is an **n8n AI Agent**. The Agent owns the
conversation, tool selection and the final answer. Python owns catalog truth.

## Decision 1: runtime ownership (amends 4A §1, §6, §7, §13)

| Concern | Owner |
|---|---|
| Conversation, intent understanding, whether catalog access is needed, which *domain operation*, tool arguments, clarification, final wording | n8n AI Agent (LLM) |
| Argument validation, SQL, pgvector, Feature Registry, ranking, effective price, availability semantics, product identity, gaps, confidence | Python Consultant Core (Phase 4B/4C, unchanged) |

4A's objection to tool calling is addressed by *what* the tools are, rather than by removing
them:

- The Agent chooses between five **domain operations** (`search_tvs`, `get_tv`, `compare_tvs`,
  `recommend_tvs`, `get_catalog_stats`). It never chooses a retrieval mechanism. There is no SQL, WHERE,
  vector, similarity, weight or ranking argument anywhere. The router invariant "anything that
  sorts can never reach a vector route" still holds inside Python.
- Every argument is validated by Python against a closed schema. Anything else is rejected, never
  coerced. The 4A G1 contract (`QueryPlanDelta`) is still the internal plan: tool arguments are
  translated into it and parsed by the unchanged `QueryPlanDelta.from_dict`.
- 4A's "handles + price placeholders + post-answer validator" (G2/§14) moves to Phase 4E. In 4D,
  grounding is enforced by the result contract, the system prompt, and an offline grounding
  scorer. See PHASE_4D_AGENT_RUNTIME.md for what that does and does not guarantee.
- The unbounded-loop objection: at most **3 domain-tool calls per user turn**, configurable
  (`CONSULTANT_MAX_TOOL_CALLS_PER_TURN`). Agent `maxIterations` = 4 in n8n (a bound on model rounds,
  not on calls: one round may carry parallel calls).
  - **Correction (Gate 4D.2B-R):** the 4D.1 assumption "one MCP session = one Agent run" is false on
    n8n 2.17.7. Agent v3 runs every tool call as a separate engine action, and the MCP Client Tool
    opens a new MCP session for each (observed live: 3 sessions for a one-call turn). A per-session
    cap never binds.
  - **Enforcement point:** the turn is carried in the endpoint URL,
    `/mcp?turn={{ $execution.id }}`: one n8n execution is one user message, and the LLM cannot
    change the URL. The Python server counts `tools/call` per turn key across sessions, atomically
    for parallel calls, and in the container (`--require-turn-key`) refuses tool calls without a
    key. An execution that processes several items (a batch Execute Workflow call) shares one
    budget: stricter, never looser. No orchestration component was added.

## Decision 2: the Agent → Python boundary: MCP Streamable HTTP vs typed HTTP tools

MCP is **not** an architectural requirement. The comparison below was made for *this* n8n
instance (2.17.7, inspected read-only) and this repository. It was prompted by the reviewer's
request to justify MCP against a minimal HTTP boundary.

### What both options need (identical)

Constraints found by read-only inspection:

- The n8n container has no Python and no repo mount.
- Its only network is `n8n-compose_default`, with gateway `172.18.0.1`.
- There is no `host.docker.internal` inside the container.
- Host INPUT policy is `ACCEPT` (`ufw` is inactive). **Binding to `0.0.0.0` on the VPS would
  therefore be public.**

Given that, *either* option needs the same things:

- a Python process on the VPS, reachable from the container. It could bind the bridge gateway IP
  (no Docker/compose edit) or run as a sidecar container on `n8n-compose_default` (compose edit);
- a read-only DB credential for it;
- a shared-secret header credential in n8n.

The infrastructure and security surface is therefore **the same**. The choice does not change
the Gate 4D.2 stop condition.

### Where they differ (verified against n8n 2.17.7 source and a disposable local n8n 2.17.7)

| Aspect | MCP Client Tool → Python MCP server | Typed HTTP (`httpRequestTool` + `$fromAI`) → Python HTTP endpoint |
|---|---|---|
| Schema the LLM sees | The **exact** closed JSON Schema served by Python. n8n converts it with `convertJsonSchemaToZod`, which we verified keeps `additionalProperties:false`, `required`, enums (e.g. the 7 panel values), `pattern`. The zod layer already rejects unknown fields and bad enums before Python | `$fromAI` supports only `string`, `number`, `boolean`, `json` (verified in `from-ai-parse-utils.js`). No enums, no integers, no min/max, no patterns, no typed arrays (`json` = open object/array, `additionalProperties:true`). Allowed values can only be described in prose |
| Optional arguments | Native: absent keys are absent | A `$fromAI` parameter without a default is required. With a default, the default value is **sent**, so Python must treat sentinel values as absent, which weakens strict validation |
| Where the contract lives | Once, in Python (`agent_tools.TOOL_SCHEMAS`). The workflow lists only tool names | Duplicated. 47 `$fromAI` fields across 5 HTTP tool nodes in the workflow JSON must be kept in sync with Python by hand |
| Workflow size | 1 tool node | 5 tool nodes |
| Per-turn call cap at the Python boundary | Per MCP session. n8n opens one MCP client per Agent run (`McpClientTool.supplyData`, closes after). We confirmed one session per node run in a local n8n | Per `{{$execution.id}}` header: explicit and slightly simpler |
| Python-side code | ~300 lines stdlib (`consultant/mcp_server.py`, incl. docs): `initialize`, `tools/list`, `tools/call`, sessions, auth, limits | ~80 lines stdlib: `POST /tools/<name>` + auth |
| Protocol risk | Must track MCP versions (supports 2024-11-05 / 2025-03-26 / 2025-06-18). Verified end to end with n8n's own client (MCP Client node in a disposable n8n 2.17.7: initialize, notification 202, GET 405 tolerated, tool calls incl. invalid-arguments and clarification results) | Plain HTTP; fewer moving parts |
| Error signalling | Domain outcomes (`invalid_arguments`, `tool_call_limit_reached`) return `isError:false` with a structured status, so the Agent reads them. Internal failures return `isError:true`, which n8n turns into an error string for the Agent (verified in `McpClientTool/utils.js`: it does not abort the run) | HTTP status codes/bodies. Equivalent |

### Decision

Use **MCP Streamable HTTP**. The deciding factor is *argument safety at the Agent layer*. With
`$fromAI`, the model is offered untyped, enum-less parameters, and the contract is duplicated in the
workflow. With MCP, it is offered the same closed schema that Python enforces, from a single
source. The extra protocol code is small (~300 lines incl. docstrings), dependency-free, unit-tested, and was verified
against the exact n8n client version in use.

The Python tool facade is **transport-agnostic** (`ConsultantTools.call(name, args, turn_id)`).
If MCP causes problems at 4D.2, a typed-HTTP adapter is a ~80-line change with no effect on
the domain layer. Validation stays in Python either way.

## Correction (Phase 4D.2 read-only network inspection)

The 4D.1 text above and in PHASE_4D_AGENT_RUNTIME.md §10 treated a host process bound only to the
bridge gateway (`172.18.0.1:8765`) as an internal-only boundary. **That conclusion was wrong for
this VPS.** A later read-only inspection showed:

- `172.18.0.1/16` is owned by `br-40f1602189e9`, the bridge of `n8n-compose_default`;
- host `INPUT` policy is `ACCEPT`, and there is no rule for that address;
- Docker's `raw PREROUTING` protects each *container* IP against non-bridge ingress, but not the
  gateway address;
- Linux delivers a packet for any local address whatever interface it arrives on (weak-host
  model; a probe from n8n to the unrelated `docker0` address `172.17.0.1` also reached the host
  stack);
- the public interface is on a shared on-link `/24`.

So hosts adjacent to the public interface could reach such a listener. Only upstream routing, not
a control on the VPS, stops the wider Internet. A private address is not a boundary.

**Accepted replacement: an internal Docker service on `n8n-compose_default` with no published
port** (reachable as `samsung-consultant:8765`). Docker's per-container `raw PREROUTING` drop then
applies to it, `FORWARD` is `DROP` for non-bridge ingress, and Traefik runs with
`exposedByDefault=false`. The MCP bearer token stays mandatory: network isolation does not replace
authentication. The MCP-versus-HTTP decision above is unaffected: the infrastructure need is
still identical for both.

## Consequences

- Nothing is deployed in Gate 4D.1. The MCP server has only run locally, against a disposable
  database and a disposable local n8n.
- **Gate 4D.2 requires a production infrastructure change and is therefore stopped.**
  ~~PHASE_4D_AGENT_RUNTIME.md §10 lists the minimal change for approval: a process bound only to
  `172.18.0.1`, a read-only role, a token credential, and one inactive workflow.~~ *Superseded by
  the correction above:* the deployed boundary is the internal Docker service (Gate 4D.2A),
  plus a read-only role, a token credential and one inactive workflow.
- Phase 4E (answer validation against evidence) and 4F (durable conversation state) stay
  separate. 4D adds neither.
