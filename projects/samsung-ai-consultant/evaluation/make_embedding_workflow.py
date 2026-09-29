"""Generate the temporary evaluation-only n8n workflow that embeds the Phase 3D queries.

    python -m evaluation.make_embedding_workflow            # writes workflows/evaluation-query-embeddings.json

Query texts are taken from the dataset (never retyped). The OpenAI node is copied from the
existing smoke-test workflow so model, credential *reference* and call shape are identical.
The workflow has no database node and is generated without an ``active`` flag.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from .dataset import load_dataset
from .run_baseline import queries_needing_embedding

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
OUT = WORKFLOWS / "evaluation-query-embeddings.json"
NAME = "Samsung — RAG Evaluation Query Embeddings"

EMIT_CODE = """// Phase 3D evaluation queries, generated from evaluation/retrieval_cases.json.
const queries = %s;
return queries.map((query_text, i) => ({ json: { query_index: i + 1, query_text } }));"""

VALIDATE_CODE = """const EXPECTED_DIMENSIONS = 1536;
const entry = ($json.data || [])[0];
const vector = entry && entry.embedding;

if (!Array.isArray(vector) || vector.length !== EXPECTED_DIMENSIONS) {
  throw new Error(
    `Query embedding: unexpected dimension ${vector ? vector.length : 'missing'} (expected ${EXPECTED_DIMENSIONS})`
  );
}

const src = $('Emit evaluation queries').item.json;
return [{
  json: {
    query_index: src.query_index,
    query_text: src.query_text,
    model: $json.model,
    dimensions: vector.length,
    embedding: vector,
  },
}];"""


def build() -> dict:
    queries = queries_needing_embedding(load_dataset())
    smoke = json.loads((WORKFLOWS / "rag-retrieval-smoke-test.json").read_text(encoding="utf-8"))
    openai = copy.deepcopy(next(n for n in smoke["nodes"] if n["name"] == "OpenAI: Embed query"))
    openai["id"] = "http-openai-embed-eval-query"
    openai["position"] = [-160, 0]
    assert openai["parameters"]["jsonBody"].count("text-embedding-3-small") == 1
    nodes = [
        {"id": "sticky-purpose", "name": "Sticky Note: Purpose", "type": "n8n-nodes-base.stickyNote",
         "typeVersion": 1, "position": [-640, -260],
         "parameters": {"width": 720, "height": 200, "content":
                        "TEMPORARY evaluation-only workflow (Phase 3D.2). Embeds the %d evaluation "
                        "queries with text-embedding-3-small and returns the vectors. No database "
                        "node, no writes. Delete after use. Not a production Samsung workflow." % len(queries)}},
        {"id": "manual-trigger", "name": "When clicking ‘Execute workflow’",
         "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "position": [-640, 0], "parameters": {}},
        {"id": "code-emit-queries", "name": "Emit evaluation queries", "type": "n8n-nodes-base.code",
         "typeVersion": 2, "position": [-400, 0],
         "parameters": {"language": "javaScript",
                        "jsCode": EMIT_CODE % json.dumps(queries, ensure_ascii=False, indent=2)}},
        openai,
        {"id": "code-validate-embedding", "name": "Validate embedding", "type": "n8n-nodes-base.code",
         "typeVersion": 2, "position": [80, 0],
         "parameters": {"language": "javaScript", "jsCode": VALIDATE_CODE}},
    ]
    chain = [n["name"] for n in nodes[1:]]
    conns = {a: {"main": [[{"node": b, "type": "main", "index": 0}]]} for a, b in zip(chain, chain[1:])}
    return {"name": NAME, "nodes": nodes, "connections": conns, "settings": {"executionOrder": "v1"}}


def main() -> int:
    OUT.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
