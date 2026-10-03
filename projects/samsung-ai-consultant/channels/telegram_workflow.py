"""Generates the n8n workflow ``TV Consultant — Telegram`` (``workflows/telegram-transport.json``).

    python -m channels.telegram_workflow [--check]

Phase 5A: Telegram is a thin transport in front of the existing Consultant workflow (``Samsung — AI Consultant``,
``workflows/ai-consultant.json``). It holds no prompt, model, memory or tool: it maps a Telegram update to the
Consultant's Execute Workflow Trigger contract ``{chatInput, sessionId}`` and sends the Agent's answer back.

    Telegram Trigger (message) -> Route update ─┬─ text ─┬─> Show typing
                                                │        └─> Ask Consultant (Execute Workflow, by id) ─┐
                                                │                       (error output) ────────────────┤
                                                │                                                      v
                                                │              Send reply (HTML) <── Format reply <────┘
                                                │                 └─ (error output) -> Send reply (plain text)
                                                └─ start / unsupported / too long -> Send notice

Routing and rendering live in ``telegram_transport.js`` (embedded into the Code nodes, tested with Node).
Credentials are referenced by ``{id, name}`` only. The Telegram Trigger verifies Telegram's secret token, which n8n
derives from the workflow id and the trigger node id; the node id is in this public repository, so the transport
workflow's id is kept out of it (git-ignored ``.meta.json`` and n8n-tool backups).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
OUT = PROJECT / "workflows" / "telegram-transport.json"
TRANSPORT_JS = Path(__file__).parent / "telegram_transport.js"
CONSULTANT_META = PROJECT / "workflows" / "ai-consultant.meta.json"

WORKFLOW_NAME = "TV Consultant — Telegram"
CONSULTANT_WORKFLOW_NAME = "Samsung — AI Consultant"
# Existing credential, created by the owner in n8n and used by no other workflow (checked in the Phase 5A preflight).
TELEGRAM_CREDENTIAL = {"id": "fPvuGxP0T6npNj0W", "name": "Telegram account Personal RAG Assistent"}
CONSULTANT_INPUTS = ("chatInput", "sessionId")   # the Consultant's Execute Workflow Trigger contract

ROUTE_NODE = "Route update"
FORMAT_NODE = "Format reply"
ROUTE_CODE = """
const routed = routeUpdate($input.first().json);
return routed ? [{ json: routed }] : [];
"""
# One update per execution, so the route item is the only one. The input is the Agent's {output}, or {error} when
# the Consultant call failed.
FORMAT_CODE = f"""
const chatId = $('{ROUTE_NODE}').first().json.chatId;
return formatReply($input.first().json).map((m) => ({{ json: {{ chatId, html: m.html, text: m.text }} }}));
"""


def _id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tv-consultant-telegram/{name}"))


def consultant_workflow_id() -> str:
    return json.loads(CONSULTANT_META.read_text(encoding="utf-8"))["remoteWorkflowId"]


def _code(name: str, key: str, snippet: str, library: str, position: list) -> dict:
    return {"id": _id(key), "name": name, "type": "n8n-nodes-base.code", "typeVersion": 2, "position": position,
            "parameters": {"mode": "runOnceForAllItems", "language": "javaScript",
                           "jsCode": library.rstrip() + "\n" + snippet}}


def _send(name: str, key: str, chat_id: str, text: str, position: list, html: bool, on_error: str = None) -> dict:
    fields = {"appendAttribution": False, "disable_web_page_preview": True}
    if html:
        fields["parse_mode"] = "HTML"
    node = {"id": _id(key), "name": name, "type": "n8n-nodes-base.telegram", "typeVersion": 1.2, "position": position,
            "parameters": {"resource": "message", "operation": "sendMessage", "chatId": chat_id, "text": text,
                           "additionalFields": fields},
            "credentials": {"telegramApi": TELEGRAM_CREDENTIAL}}
    if on_error:
        node["onError"] = on_error
    return node


def build(library: str, consultant_id: str) -> dict:
    schema = [{"id": k, "displayName": k, "required": False, "defaultMatch": False, "display": True,
               "canBeUsedToMatch": True, "type": "string", "removed": False} for k in CONSULTANT_INPUTS]
    nodes = [
        {"id": _id("telegram-trigger"), "name": "Telegram Trigger", "webhookId": _id("telegram-webhook"),
         "type": "n8n-nodes-base.telegramTrigger", "typeVersion": 1.2, "position": [-700, 0],
         "parameters": {"updates": ["message"], "additionalFields": {}},
         "credentials": {"telegramApi": TELEGRAM_CREDENTIAL}},
        _code(ROUTE_NODE, "route", ROUTE_CODE, library, [-480, 0]),
        {"id": _id("is-text"), "name": "Is text?", "type": "n8n-nodes-base.if", "typeVersion": 2.2,
         "position": [-260, 0],
         "parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict",
                                                   "version": 2},
                                       "conditions": [{"id": _id("is-text-condition"), "leftValue": "={{ $json.kind }}",
                                                       "rightValue": "text",
                                                       "operator": {"type": "string", "operation": "equals"}}],
                                       "combinator": "and"},
                        "options": {}}},
        # Placed above "Ask Consultant": execution order v1 runs it first, so the user sees "typing…" while the Agent
        # works. Its failure never stops the turn.
        {"id": _id("typing"), "name": "Show typing", "type": "n8n-nodes-base.telegram", "typeVersion": 1.2,
         "position": [0, -200], "onError": "continueRegularOutput",
         "parameters": {"resource": "message", "operation": "sendChatAction", "chatId": "={{ $json.chatId }}",
                        "action": "typing"},
         "credentials": {"telegramApi": TELEGRAM_CREDENTIAL}},
        {"id": _id("ask-consultant"), "name": "Ask Consultant", "type": "n8n-nodes-base.executeWorkflow",
         "typeVersion": 1.2, "position": [0, 0], "onError": "continueErrorOutput",
         "parameters": {"source": "database",
                        "workflowId": {"__rl": True, "value": consultant_id, "mode": "list",
                                       "cachedResultName": CONSULTANT_WORKFLOW_NAME},
                        "workflowInputs": {"mappingMode": "defineBelow",
                                           "value": {k: "={{ $json.%s }}" % k for k in CONSULTANT_INPUTS},
                                           "matchingColumns": [], "schema": schema,
                                           "attemptToConvertTypes": False, "convertFieldsToString": True},
                        "mode": "once", "options": {"waitForSubWorkflow": True}}},
        _code(FORMAT_NODE, "format", FORMAT_CODE, library, [240, 0]),
        _send("Send reply", "send-reply", "={{ $json.chatId }}", "={{ $json.html }}", [480, 0], html=True,
              on_error="continueErrorOutput"),
        _send("Send reply (plain text)", "send-plain", "={{ $('%s').item.json.chatId }}" % FORMAT_NODE,
              "={{ $('%s').item.json.text }}" % FORMAT_NODE, [720, 120], html=False),
        _send("Send notice", "send-notice", "={{ $json.chatId }}", "={{ $json.reply }}", [0, 240], html=False),
    ]
    connections = {
        "Telegram Trigger": {"main": [[{"node": ROUTE_NODE, "type": "main", "index": 0}]]},
        ROUTE_NODE: {"main": [[{"node": "Is text?", "type": "main", "index": 0}]]},
        "Is text?": {"main": [[{"node": "Show typing", "type": "main", "index": 0},
                               {"node": "Ask Consultant", "type": "main", "index": 0}],
                              [{"node": "Send notice", "type": "main", "index": 0}]]},
        # Output 0: the Agent's answer; output 1 (error): the Consultant failed -> the "unavailable" reply.
        "Ask Consultant": {"main": [[{"node": FORMAT_NODE, "type": "main", "index": 0}],
                                    [{"node": FORMAT_NODE, "type": "main", "index": 0}]]},
        FORMAT_NODE: {"main": [[{"node": "Send reply", "type": "main", "index": 0}]]},
        # Output 1 (error): Telegram rejected the HTML -> the same part without markup.
        "Send reply": {"main": [[], [{"node": "Send reply (plain text)", "type": "main", "index": 0}]]},
    }
    settings = {"executionOrder": "v1", "saveManualExecutions": True, "saveDataSuccessExecution": "all",
                "saveDataErrorExecution": "all"}
    return {"name": WORKFLOW_NAME, "nodes": nodes, "connections": connections, "settings": settings}


def render() -> str:
    library = TRANSPORT_JS.read_text(encoding="utf-8")
    return json.dumps(build(library, consultant_workflow_id()), ensure_ascii=False, indent=2) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if the committed workflow JSON is stale")
    ns = ap.parse_args(argv)
    text = render()
    if ns.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT} is stale: run python -m channels.telegram_workflow", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(PROJECT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
