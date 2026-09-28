"""Structural validation of a workflow definition, before it is deployed.

Deliberately separate from secret scanning (n8n_tool.sanitizer): validate()
covers JSON/structural correctness, while secret detection is its own
explicit pipeline step (see task spec — "validate" and "secret scan" are
listed as two distinct stages), even though the `workflows validate` CLI
command reports both together for convenience.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .normalizer import count_connections, count_nodes, find_extra_top_level_fields
from .sanitizer import SecretFinding, scan_workflow_for_secrets


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    secret_findings: list[SecretFinding] = field(default_factory=list)
    node_count: int = 0
    connection_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and not self.secret_findings


def validate_workflow(workflow: Any) -> ValidationResult:
    """Validate a parsed workflow dict's structure and scan it for secrets.

    Does not touch the network and does not require a JSON string — callers
    that have a file path should parse JSON themselves and report a
    dedicated "invalid JSON" error on failure (see cli.py), since a JSON
    parse error isn't a property of a workflow dict.
    """
    result = ValidationResult()

    if not isinstance(workflow, dict):
        result.errors.append("Workflow must be a JSON object")
        return result

    name = workflow.get("name")
    if not isinstance(name, str) or not name.strip():
        result.errors.append("Workflow is missing a non-empty 'name'")

    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        result.errors.append("'nodes' must be an array")
        nodes = []

    connections = workflow.get("connections")
    if not isinstance(connections, dict):
        result.errors.append("'connections' must be an object")
        connections = {}

    if "settings" not in workflow:
        result.warnings.append("'settings' is missing; deploy will default it to {}")
    elif not isinstance(workflow.get("settings"), dict):
        result.errors.append("'settings' must be an object")

    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    node_names: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            result.errors.append(f"nodes[{index}] is not an object")
            continue

        node_id = node.get("id")
        node_name = node.get("name")

        if not node_id:
            result.errors.append(f"nodes[{index}] is missing 'id'")
        elif node_id in seen_ids:
            result.errors.append(f"duplicate node id {node_id!r}")
        else:
            seen_ids.add(node_id)

        if not node_name:
            result.errors.append(f"nodes[{index}] is missing 'name'")
        elif node_name in seen_names:
            result.errors.append(f'duplicate node name "{node_name}"')
        else:
            seen_names.add(node_name)
            node_names.add(node_name)

        if not node.get("type"):
            result.errors.append(f"nodes[{index}] ({node_name or '?'}) is missing 'type'")
        if "position" not in node:
            result.errors.append(f"nodes[{index}] ({node_name or '?'}) is missing 'position'")
        if "typeVersion" not in node:
            result.warnings.append(f"nodes[{index}] ({node_name or '?'}) is missing 'typeVersion'")
        if "parameters" not in node:
            result.warnings.append(f"nodes[{index}] ({node_name or '?'}) is missing 'parameters'")

    for source_name, output_types in connections.items():
        if source_name not in node_names:
            result.errors.append(f'connection source references missing node "{source_name}"')
        if not isinstance(output_types, dict):
            result.errors.append(f'connections["{source_name}"] must be an object')
            continue
        for output_type, output_slots in output_types.items():
            if not isinstance(output_slots, list):
                continue
            for slot_index, edges in enumerate(output_slots):
                if not isinstance(edges, list):
                    continue
                for edge in edges:
                    if not isinstance(edge, dict):
                        continue
                    target_name = edge.get("node")
                    if target_name not in node_names:
                        result.errors.append(
                            f'connection references missing node "{target_name}" '
                            f'(from "{source_name}".{output_type}[{slot_index}])'
                        )

    result.secret_findings = scan_workflow_for_secrets(workflow)

    extra_fields = find_extra_top_level_fields(workflow)
    if extra_fields:
        result.warnings.append(
            "server-controlled field(s) present and will be ignored on deploy: "
            + ", ".join(sorted(extra_fields))
        )

    result.node_count = count_nodes(workflow)
    result.connection_count = count_connections(workflow)
    return result
