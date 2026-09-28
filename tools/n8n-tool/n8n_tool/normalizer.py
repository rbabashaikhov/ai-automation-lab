"""Mapping between workflow representations, and counting helpers.

    RAW REMOTE / local file
            |
      build_create_payload / build_update_payload
            v
    DEPLOYABLE WORKFLOW  (only fields the n8n API accepts as input)

The n8n public API's ``workflow``/``workflowCreate`` request schemas both
declare ``additionalProperties: false`` (confirmed by reading this
instance's own OpenAPI spec at GET /api/v1/openapi.yml — not guessed), so
any field outside these allowlists causes a 400. Create and update use two
different schemas with slightly different writable field sets:

- POST /api/v1/workflows  (schema: workflowCreate) — no ``description``,
  has ``projectId``.
- PUT /api/v1/workflows/{id}  (schema: workflow) — has ``description``, no
  ``projectId``.

Both require: name, nodes, connections, settings. Server-controlled fields
(id, active, createdAt, updatedAt, isArchived, versionId, triggerCount,
meta, tags, shared, activeVersion, activeVersionId) are never sent — the
API ignores/rejects them, and activation is a separate endpoint this tool
does not call.
"""

from __future__ import annotations

from typing import Any

# Fields kept on each node object (matches the `node` schema's writable
# properties; createdAt/updatedAt are readOnly and dropped).
NODE_FIELDS = (
    "id",
    "name",
    "webhookId",
    "disabled",
    "notesInFlow",
    "notes",
    "type",
    "typeVersion",
    "executeOnce",
    "alwaysOutputData",
    "retryOnFail",
    "maxTries",
    "waitBetweenTries",
    "continueOnFail",
    "onError",
    "position",
    "parameters",
    "credentials",
)

# Top-level fields writable on create (workflowCreate schema).
CREATE_FIELDS = ("name", "nodes", "connections", "settings", "staticData", "pinData")

# Top-level fields writable on update (workflow schema).
UPDATE_FIELDS = ("name", "description", "nodes", "connections", "settings", "staticData", "pinData")

# Top-level fields that exist on a workflow object but are server-controlled
# and never sent in a create/update payload. Used only to give validate()
# something to warn about.
READ_ONLY_TOP_LEVEL_FIELDS = (
    "id",
    "active",
    "createdAt",
    "updatedAt",
    "isArchived",
    "versionId",
    "triggerCount",
    "meta",
    "tags",
    "shared",
    "activeVersion",
    "activeVersionId",
)


def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    return {field: node[field] for field in NODE_FIELDS if field in node}


def _normalize_nodes(nodes: Any) -> list[dict[str, Any]]:
    if not isinstance(nodes, list):
        return []
    return [_normalize_node(node) for node in nodes if isinstance(node, dict)]


def _base_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": workflow.get("name", ""),
        "nodes": _normalize_nodes(workflow.get("nodes")),
        "connections": workflow.get("connections") or {},
        # settings is required by the API; default to {} if the local file
        # omits it rather than reject an otherwise-valid file outright
        # (validate() warns about this).
        "settings": workflow.get("settings") or {},
    }
    if workflow.get("staticData") is not None:
        payload["staticData"] = workflow["staticData"]
    if workflow.get("pinData") is not None:
        payload["pinData"] = workflow["pinData"]
    return payload


def build_create_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    """Map a local/remote workflow dict to a POST /api/v1/workflows body."""
    return _base_payload(workflow)


def build_update_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    """Map a local/remote workflow dict to a PUT /api/v1/workflows/{id} body."""
    payload = _base_payload(workflow)
    if workflow.get("description"):
        payload["description"] = workflow["description"]
    return payload


def count_nodes(workflow: dict[str, Any]) -> int:
    nodes = workflow.get("nodes")
    return len(nodes) if isinstance(nodes, list) else 0


def count_connections(workflow: dict[str, Any]) -> int:
    """Count individual connection edges in the n8n ``connections`` structure.

    Shape: {source_node: {output_type: [[{node, type, index}, ...], ...]}}.
    """
    connections = workflow.get("connections")
    if not isinstance(connections, dict):
        return 0
    total = 0
    for output_types in connections.values():
        if not isinstance(output_types, dict):
            continue
        for output_slots in output_types.values():
            if not isinstance(output_slots, list):
                continue
            for edges in output_slots:
                if isinstance(edges, list):
                    total += len(edges)
    return total


def find_extra_top_level_fields(workflow: dict[str, Any]) -> list[str]:
    """Read-only/server fields present in a local file that deploy will drop."""
    return [field for field in READ_ONLY_TOP_LEVEL_FIELDS if field in workflow]
