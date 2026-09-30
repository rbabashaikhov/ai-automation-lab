"""Generates the n8n workflow ``Samsung — AI Consultant`` (``workflows/ai-consultant.json``).

    python -m consultant.n8n_workflow [--check]

Generated rather than hand-edited so the Agent system prompt (``prompts/agent_system_v3.md``) and
the tool list (``agent_tools.TOOL_SCHEMAS``) have a single source; ``--check`` fails when the
committed JSON is stale (also asserted by the unit tests). Deployed with ``tools/n8n-tool``.

Runtime shape (Phase 4D, development only -- never activated, no public webhook):

    Chat Trigger (editor chat, public=false) ─┐
    Execute Workflow Trigger (evaluation) ────┴─> AI Agent ── OpenAI Chat Model (existing credential)
                                                     ├──────── Window memory (in-process, per sessionId)
                                                     └──────── MCP Client Tool "catalog" -> Python MCP server

Credentials are referenced by ``{id, name}`` only (never values). The MCP Header Auth credential
was created in n8n in Gate 4D.2A; its secret lives only in n8n and in the VPS env file.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from .agent_tools import DEFAULT_MAX_TOOL_CALLS_PER_TURN, TOOL_NAMES

PROJECT = Path(__file__).resolve().parents[1]
OUT = PROJECT / "workflows" / "ai-consultant.json"
# v2 (Gate 4D.2B-R): catalog claims need a tool result first; use case vs required features; group claims;
# bright-room gap. v3 (Gate 4D.2D): price display for discounted products, no quality comparatives, overview /
# comparison without attribute lists, self-correct invalid_arguments. v1 and v2 are kept for the 4D.2B / 4D.2C records.
PROMPT_FILE = Path(__file__).parent / "prompts" / "agent_system_v3.md"

WORKFLOW_NAME = "Samsung — AI Consultant"
AGENT_MODEL = "gpt-4.1-mini"          # measured via this credential in Phase 3D; temperature 0
OPENAI_CREDENTIAL = {"id": "mcixQy0sFVXl7nU9", "name": "OpenAI account"}
MCP_CREDENTIAL = {"id": "9Ak6Ely4Wbp63RUn", "name": "Samsung Consultant MCP"}   # Header Auth; value only in n8n
# Internal Docker service on n8n-compose_default (Phase 4D.2A; deploy/consultant/compose.yml), reached by
# service name -- never a container IP and never the bridge gateway (see ADR 004 correction).
MCP_ENDPOINT = "http://samsung-consultant:8765/mcp"
# Agent v3 runs each tool call as its own engine action with a new MCP session, so the turn is carried
# in the URL: one n8n execution = one user message. The server counts tools/call per this key
# (--require-turn-key in the container), across sessions and parallel calls (Gate 4D.2B-R).
# Gate 4E.2: the semantic guard also gets the conversation key and the user's message. In the Agent's sub-nodes
# $json is the Agent's input item ({chatInput, sessionId} from either trigger; the memory node already keys on
# $json.sessionId). The server keeps only derived evidence and redacts q from its request log.
# Gate 4E.2A: h = earlier turns in the Agent's memory ("Prior turns" reads the same window memory before the Agent
# runs). The guard removes nothing unless the Consultant has seen at least that many turns of the conversation; an
# empty h (node not run / failed) also means report-only. Count the message groups that hold a user message:
# messagesCount counts groups, and a turn with a tool call is stored as two ({human, ai, tool}, {ai}).
PRIOR_TURNS_NODE = "Prior turns"
MCP_ENDPOINT_EXPRESSION = ("=" + MCP_ENDPOINT + "?turn={{ $execution.id }}&conv={{ encodeURIComponent($json.sessionId) }}"
                           "&q={{ encodeURIComponent($json.chatInput) }}"
                           "&h={{ $('" + PRIOR_TURNS_NODE + "').isExecuted && Array.isArray($('" + PRIOR_TURNS_NODE
                           + "').first().json.messages) ? $('" + PRIOR_TURNS_NODE
                           + "').first().json.messages.filter(g => g.human !== undefined).length : '' }}")
MCP_NODE_NAME = "catalog"              # n8n exposes tools as "<node name>_<tool>", e.g. catalog_search_tvs
# One agent step per tool round plus the final answer (a bound on rounds, not calls); the per-turn call cap
# is enforced by Python per turn key.
AGENT_MAX_ITERATIONS = DEFAULT_MAX_TOOL_CALLS_PER_TURN + 1
MEMORY_WINDOW = 6


def _id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"samsung-ai-consultant/{name}"))


def build(prompt: str) -> dict:
    agent = "Samsung AI Consultant"
    nodes = [
        {"id": _id("chat-trigger"), "name": "When chat message received", "webhookId": _id("chat-webhook"),
         "type": "@n8n/n8n-nodes-langchain.chatTrigger", "typeVersion": 1.4, "position": [-460, -120],
         "parameters": {"public": False, "options": {}}},
        {"id": _id("eval-trigger"), "name": "When called by evaluation workflow",
         "type": "n8n-nodes-base.executeWorkflowTrigger", "typeVersion": 1.1, "position": [-460, 100],
         "parameters": {"workflowInputs": {"values": [{"name": "chatInput", "type": "string"},
                                                      {"name": "sessionId", "type": "string"}]}}},
        {"id": _id("agent"), "name": agent, "type": "@n8n/n8n-nodes-langchain.agent", "typeVersion": 3.1,
         "position": [-120, 0],
         "parameters": {"promptType": "define", "text": "={{ $json.chatInput }}",
                        "options": {"systemMessage": prompt, "maxIterations": AGENT_MAX_ITERATIONS,
                                    "returnIntermediateSteps": True}}},
        {"id": _id("openai-model"), "name": "OpenAI Chat Model", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
         "typeVersion": 1.3, "position": [-260, 240],
         "parameters": {"model": {"__rl": True, "mode": "list", "value": AGENT_MODEL, "cachedResultName": AGENT_MODEL},
                        "options": {"temperature": 0}},
         "credentials": {"openAiApi": OPENAI_CREDENTIAL}},
        {"id": _id("memory"), "name": "Window memory (per session)",
         "type": "@n8n/n8n-nodes-langchain.memoryBufferWindow", "typeVersion": 1.3, "position": [-100, 240],
         "parameters": {"sessionIdType": "customKey", "sessionKey": "={{ $json.sessionId }}",
                        "contextWindowLength": MEMORY_WINDOW}},
        # Placed above the Agent: execution order v1 runs this branch of the trigger first, so it reads the memory
        # before the Agent adds the current turn. One grouped item {messages, messagesCount}; errors do not stop the turn.
        {"id": _id("prior-turns"), "name": PRIOR_TURNS_NODE, "type": "@n8n/n8n-nodes-langchain.memoryManager",
         "typeVersion": 1.1, "position": [-120, -240], "onError": "continueRegularOutput",
         "parameters": {"mode": "load", "simplifyOutput": True, "options": {"groupMessages": True}}},
        {"id": _id("mcp-tool"), "name": MCP_NODE_NAME, "type": "@n8n/n8n-nodes-langchain.mcpClientTool",
         "typeVersion": 1.2, "position": [60, 240],
         "parameters": {"endpointUrl": MCP_ENDPOINT_EXPRESSION, "serverTransport": "httpStreamable",
                        "authentication": "headerAuth", "include": "selected", "includeTools": list(TOOL_NAMES),
                        "options": {"timeout": 30000}},
         "credentials": {"httpHeaderAuth": MCP_CREDENTIAL}},
    ]
    connections = {
        "When chat message received": {"main": [[{"node": PRIOR_TURNS_NODE, "type": "main", "index": 0},
                                                  {"node": agent, "type": "main", "index": 0}]]},
        "When called by evaluation workflow": {"main": [[{"node": PRIOR_TURNS_NODE, "type": "main", "index": 0},
                                                         {"node": agent, "type": "main", "index": 0}]]},
        "OpenAI Chat Model": {"ai_languageModel": [[{"node": agent, "type": "ai_languageModel", "index": 0}]]},
        "Window memory (per session)": {"ai_memory": [[{"node": agent, "type": "ai_memory", "index": 0},
                                                       {"node": PRIOR_TURNS_NODE, "type": "ai_memory", "index": 0}]]},
        MCP_NODE_NAME: {"ai_tool": [[{"node": agent, "type": "ai_tool", "index": 0}]]},
    }
    settings = {"executionOrder": "v1", "saveManualExecutions": True, "saveDataSuccessExecution": "all",
                "saveDataErrorExecution": "all", "callerPolicy": "workflowsFromSameOwner"}
    return {"name": WORKFLOW_NAME, "nodes": nodes, "connections": connections, "settings": settings}


def render() -> str:
    return json.dumps(build(PROMPT_FILE.read_text(encoding="utf-8").strip()), ensure_ascii=False, indent=2) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if the committed workflow JSON is stale")
    ns = ap.parse_args(argv)
    text = render()
    if ns.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT} is stale: run python -m consultant.n8n_workflow", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(PROJECT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
