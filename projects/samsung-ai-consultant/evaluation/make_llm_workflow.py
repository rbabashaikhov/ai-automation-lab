"""Generate the temporary evaluation-only n8n workflow for the Phase 3D.3 consultant spike.

    python -m evaluation.make_llm_workflow    # writes workflows/evaluation-consultant-spike.json
    python -m evaluation.make_llm_workflow fullcatalog   # Phase 3D.4 contexts -> workflows/evaluation-fullcatalog-spike.json

Uses exactly the prompts stored in evaluation/results/consultant_spike_contexts.json (system prompt
and the five user messages) and makes exactly one chat completion per prompt. No embeddings call,
no database node. The OpenAI credential is referenced (copied from the existing evaluation
workflow), never embedded. Generated without an ``active`` flag.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from .consultant_context import OUT as CONTEXTS
from .fullcatalog_context import OUT as FULLCATALOG_CONTEXTS

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
OUT = WORKFLOWS / "evaluation-consultant-spike.json"
NAME = "Samsung — RAG Evaluation Consultant Spike (temporary)"
VARIANTS = {  # name -> (contexts file, output file, phase label)
    "spike": (CONTEXTS, OUT, "Phase 3D.3 consultant spike"),
    "fullcatalog": (FULLCATALOG_CONTEXTS, WORKFLOWS / "evaluation-fullcatalog-spike.json",
                    "Phase 3D.4 full-catalog control"),
}
MODEL = "gpt-4.1-mini"

EMIT_CODE = """// Phase 3D.3 spike prompts, generated from evaluation/results/consultant_spike_contexts.json.
const system_prompt = %s;
const cases = %s;
return cases.map((c, i) => ({ json: { query_index: i + 1, query_text: c.query, system_prompt, user_message: c.user_message } }));"""

# Per-item mode: each of the 5 chat responses is shaped independently.
SHAPE_CODE = """const src = $('Emit spike prompts').item.json;
const choice = ($json.choices || [])[0];
const answer = choice && choice.message && choice.message.content;
if (typeof answer !== 'string' || !answer.trim()) {
  throw new Error(`Empty chat completion for query ${src.query_index}`);
}
return {
  json: {
    query_index: src.query_index,
    query_text: src.query_text,
    model: $json.model,
    finish_reason: choice.finish_reason,
    usage: $json.usage,
    answer,
  },
};"""


def build(variant: str = "spike") -> dict:
    contexts, _, label = VARIANTS[variant]
    data = json.loads(contexts.read_text(encoding="utf-8"))
    cases = [{"query": c["query"], "user_message": c["user_message"]} for c in data["cases"]]
    assert len(cases) == 5
    base = json.loads((WORKFLOWS / "evaluation-query-embeddings.json").read_text(encoding="utf-8"))
    http = copy.deepcopy(next(n for n in base["nodes"] if n["name"] == "OpenAI: Embed query"))
    http["id"] = "http-openai-chat-spike"
    http["name"] = "OpenAI: Chat completion"
    http["position"] = [-160, 0]
    http["parameters"]["url"] = "https://api.openai.com/v1/chat/completions"
    http["parameters"]["jsonBody"] = (
        '={{ JSON.stringify({ model: "%s", temperature: 0, messages: ['
        '{ role: "system", content: $json.system_prompt }, '
        '{ role: "user", content: $json.user_message }] }) }}' % MODEL)
    nodes = [
        {"id": "sticky-purpose", "name": "Sticky Note: Purpose", "type": "n8n-nodes-base.stickyNote",
         "typeVersion": 1, "position": [-640, -260],
         "parameters": {"width": 720, "height": 200, "content":
                        "TEMPORARY evaluation-only workflow (%s). Sends 5 pre-built "
                        "catalog-context prompts to %s (temperature 0) and returns the answers. Exactly 5 chat "
                        "calls, no embeddings, no database node, no writes. Delete after use." % (label, MODEL)}},
        {"id": "manual-trigger", "name": "When clicking ‘Execute workflow’",
         "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "position": [-640, 0], "parameters": {}},
        {"id": "code-emit-prompts", "name": "Emit spike prompts", "type": "n8n-nodes-base.code",
         "typeVersion": 2, "position": [-400, 0],
         "parameters": {"language": "javaScript", "jsCode": EMIT_CODE % (
             json.dumps(data["system_prompt"], ensure_ascii=False),
             json.dumps(cases, ensure_ascii=False, indent=1))}},
        http,
        {"id": "code-shape-answer", "name": "Shape answer", "type": "n8n-nodes-base.code",
         "typeVersion": 2, "position": [80, 0],
         "parameters": {"mode": "runOnceForEachItem", "language": "javaScript", "jsCode": SHAPE_CODE}},
    ]
    chain = [n["name"] for n in nodes[1:]]
    conns = {a: {"main": [[{"node": b, "type": "main", "index": 0}]]} for a, b in zip(chain, chain[1:])}
    return {"name": NAME, "nodes": nodes, "connections": conns, "settings": {"executionOrder": "v1"}}


def main(argv=None) -> int:
    variant = (argv if argv is not None else sys.argv[1:] or ["spike"])[0]
    out = VARIANTS[variant][1]
    out.write_text(json.dumps(build(variant), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
